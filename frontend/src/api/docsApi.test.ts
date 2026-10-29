import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  DOC_NOTES_FEATURE,
  acceptDocNoteSuggestion,
  addDocMemory,
  deleteDocMemory,
  dismissDocNoteSuggestion,
  docAssetBase,
  docAssetUrl,
  docDownloadUrl,
  duplicateMemoryId,
  fetchConversationDocNoteSuggestions,
  fetchDocNoteSuggestions,
  fetchDocNotes,
  fetchDocs,
  isStaleInstructionsError,
  isStaleUpdateError,
  saveDocInstructions,
  staleInstructionsCurrent,
  staleUpdateCurrent,
  updateDocMemory,
} from './docsApi';
import { ApiClientError, handleErrorResponse } from './request';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

async function errorFrom(status: number, body: unknown): Promise<unknown> {
  try {
    await handleErrorResponse(jsonResponse(status, body));
  } catch (err) {
    return err;
  }
  throw new Error('handleErrorResponse did not throw');
}

describe('docsApi', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('recognizes the flat stale_update 409 and returns its current row', async () => {
    const current = { id: 'd1', title: 'Theirs', updated_at: '2026-10-02T00:00:00' };
    const err = await errorFrom(409, { error: 'stale_update', message: 'Changed', current });
    expect(isStaleUpdateError(err)).toBe(true);
    expect(staleUpdateCurrent(err)).toEqual(current);
  });

  it('does not treat other errors as stale updates', async () => {
    const duplicate = await errorFrom(409, {
      detail: { error: 'duplicate_title', message: 'Taken' },
    });
    expect((duplicate as ApiClientError).errorCode).toBe('duplicate_title');
    expect(isStaleUpdateError(duplicate)).toBe(false);
    expect(staleUpdateCurrent(duplicate)).toBeNull();
    expect(isStaleUpdateError(new Error('stale_update'))).toBe(false);
    expect(staleUpdateCurrent(new ApiClientError('x', 409, 'stale_update'))).toBeNull();
  });

  it('builds the cookie-authed asset and download URLs', () => {
    expect(docAssetBase('d1')).toBe('/app/api/docs/d1/assets');
    expect(docAssetUrl('d1', 'chart 1.png')).toBe('/app/api/docs/d1/assets/chart%201.png');
    expect(docDownloadUrl('d1', 'md')).toBe('/app/api/docs/d1/download?format=md');
    expect(docDownloadUrl('d1', 'zip')).toBe('/app/api/docs/d1/download?format=zip');
  });

  it('maps list options onto the query string', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, { docs: [], has_more: false, next_cursor: null }));
    vi.stubGlobal('fetch', fetchMock);

    await fetchDocs();
    await fetchDocs({ projectId: 'p1', limit: 5, cursor: '2026-10-01T00:00:00|d9' });

    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(calls[0][0]).toBe('/app/api/docs');
    expect(calls[1][0]).toBe(
      '/app/api/docs?project_id=p1&limit=5&cursor=2026-10-01T00%3A00%3A00%7Cd9',
    );
  });

  describe('doc notes', () => {
    /** Stub fetch with one canned 200 and return the recorded calls. */
    function stubFetch(body: unknown = {}) {
      const fetchMock = vi.fn(async () => jsonResponse(200, body));
      vi.stubGlobal('fetch', fetchMock);
      return () => fetchMock.mock.calls as unknown as [string, RequestInit][];
    }

    function sent(init: RequestInit): unknown {
      return init.body === undefined ? undefined : JSON.parse(init.body as string);
    }

    it('names the doc_notes gate', () => {
      expect(DOC_NOTES_FEATURE).toBe('doc_notes');
    });

    it('reads and writes the notes on the doc\'s routes, ids encoded', async () => {
      const calls = stubFetch();

      await fetchDocNotes('d/1');
      await saveDocInstructions('d/1', 'Cite sources.', 3);
      await addDocMemory('d/1', 'Uses metric units');
      await updateDocMemory('d/1', 'm 1', 'Uses SI units');
      await deleteDocMemory('d/1', 'm 1');

      expect(calls().map(([url, init]) => [init.method, url, sent(init)])).toEqual([
        ['GET', '/app/api/docs/d%2F1/notes', undefined],
        [
          'PUT',
          '/app/api/docs/d%2F1/instructions',
          { instructions: 'Cite sources.', expected_instructions_version: 3 },
        ],
        ['POST', '/app/api/docs/d%2F1/memories', { content: 'Uses metric units' }],
        ['PUT', '/app/api/docs/d%2F1/memories/m%201', { content: 'Uses SI units' }],
        ['DELETE', '/app/api/docs/d%2F1/memories/m%201', undefined],
      ]);
      expect(calls().every(([, init]) => init.credentials === 'include')).toBe(true);
    });

    it('lists and dismisses suggestions on the review routes, ids encoded', async () => {
      const calls = stubFetch({ suggestions: [], pending_count: 0 });

      await fetchDocNoteSuggestions('d/1');
      await fetchConversationDocNoteSuggestions('c/1');
      await dismissDocNoteSuggestion('s/1');

      expect(calls().map(([url, init]) => [init.method, url, sent(init)])).toEqual([
        ['GET', '/app/api/docs/d%2F1/note-suggestions', undefined],
        ['GET', '/app/api/conversations/c%2F1/doc-note-suggestions', undefined],
        ['POST', '/app/api/doc-note-suggestions/s%2F1/dismiss', undefined],
      ]);
    });

    it('sends only the accept options given', async () => {
      const calls = stubFetch({ suggestion: {}, memory: null, notes: {} });

      await acceptDocNoteSuggestion('s/1');
      await acceptDocNoteSuggestion('s1', { content: 'Edited' });
      await acceptDocNoteSuggestion('s1', { expectedInstructionsVersion: 0 });
      await acceptDocNoteSuggestion('s1', { content: '', expectedInstructionsVersion: 4 });

      expect(calls().map(([url, init]) => [init.method, url, sent(init)])).toEqual([
        ['POST', '/app/api/doc-note-suggestions/s%2F1/accept', {}],
        ['POST', '/app/api/doc-note-suggestions/s1/accept', { content: 'Edited' }],
        ['POST', '/app/api/doc-note-suggestions/s1/accept', { expected_instructions_version: 0 }],
        [
          'POST',
          '/app/api/doc-note-suggestions/s1/accept',
          { content: '', expected_instructions_version: 4 },
        ],
      ]);
    });

    it('recognizes the flat stale_instructions 409 and returns its current text', async () => {
      const current = { instructions: 'Theirs', instructions_version: 7 };
      const err = await errorFrom(409, { error: 'stale_instructions', message: 'Changed', current });
      expect((err as ApiClientError).errorCode).toBe('stale_instructions');
      expect(isStaleInstructionsError(err)).toBe(true);
      expect(staleInstructionsCurrent(err)).toEqual(current);
      expect(isStaleUpdateError(err)).toBe(false);
      expect(duplicateMemoryId(err)).toBeNull();
    });

    it('reads the existing memory id of a duplicate_memory 409', async () => {
      const err = await errorFrom(409, {
        detail: { error: 'duplicate_memory', message: 'Already noted', memory_id: 'm7' },
      });
      expect((err as ApiClientError).errorCode).toBe('duplicate_memory');
      expect(duplicateMemoryId(err)).toBe('m7');
      expect(isStaleInstructionsError(err)).toBe(false);
      expect(staleInstructionsCurrent(err)).toBeNull();
    });

    it('returns false / null for anything else', async () => {
      const capped = await errorFrom(409, {
        detail: { error: 'memories_cap_reached', message: 'Full', memory_id: 'm7' },
      });
      expect(isStaleInstructionsError(capped)).toBe(false);
      expect(duplicateMemoryId(capped)).toBeNull();

      for (const other of [new Error('stale_instructions'), null, undefined, 'duplicate_memory', {}]) {
        expect(isStaleInstructionsError(other)).toBe(false);
        expect(staleInstructionsCurrent(other)).toBeNull();
        expect(duplicateMemoryId(other)).toBeNull();
      }

      // The right code with a missing or malformed payload.
      expect(staleInstructionsCurrent(new ApiClientError('x', 409, 'stale_instructions'))).toBeNull();
      expect(staleInstructionsCurrent(new ApiClientError('x', 409, 'stale_instructions', undefined, {
        current: { instructions: 'Theirs' },
      }))).toBeNull();
      expect(duplicateMemoryId(new ApiClientError('x', 409, 'duplicate_memory'))).toBeNull();
      expect(duplicateMemoryId(new ApiClientError('x', 409, 'duplicate_memory', undefined, {
        detail: 'Already noted',
      }))).toBeNull();
      expect(duplicateMemoryId(new ApiClientError('x', 409, 'duplicate_memory', undefined, {
        detail: { error: 'duplicate_memory', memory_id: 7 },
      }))).toBeNull();
      // A flat body is not the duplicate_memory shape.
      expect(duplicateMemoryId(new ApiClientError('x', 409, 'duplicate_memory', undefined, {
        error: 'duplicate_memory', memory_id: 'm7',
      }))).toBeNull();
    });
  });
});
