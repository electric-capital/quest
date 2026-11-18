// The AutoProjects API client: each call's method, URL and JSON body, the
// tab-local "deleting" mark the hooks consult, and the accessors for the
// structured error extras (stale_brief's `current`, autoproject_run_active's
// `active_run`).
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  answerAutoProjectQuestion,
  archiveAutoProject,
  autoprojectActiveRunFromError,
  createAutoProject,
  createAutoProjectRunConversation,
  deleteAutoProject,
  deleteAutoProjectRun,
  getAutoProject,
  getAutoProjectBriefVersion,
  isAutoProjectDeleting,
  isStaleBriefError,
  listAutoProjectBriefVersions,
  listAutoProjectRuns,
  listAutoProjects,
  markAutoProjectRead,
  parseAutoProjectActiveRun,
  putAutoProjectBrief,
  restoreAutoProjectBriefVersion,
  staleBriefCurrent,
  unarchiveAutoProject,
  updateAutoProject,
} from './autoprojectsApi';
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

function stubFetch(response: () => Response = () => jsonResponse(200, {})) {
  const fetchMock = vi.fn<(url: string, init: RequestInit) => Promise<Response>>(async () => response());
  vi.stubGlobal('fetch', fetchMock);
  return {
    last: () => {
      const [url, init] = fetchMock.mock.calls[fetchMock.mock.calls.length - 1];
      return {
        url,
        method: init.method,
        body: init.body === undefined ? undefined : JSON.parse(init.body as string),
        credentials: init.credentials,
      };
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('autoprojectsApi requests', () => {
  it.each([
    ['list', () => listAutoProjects(), 'GET', '/app/api/autoprojects', undefined],
    ['list archived', () => listAutoProjects({ archived: true }), 'GET', '/app/api/autoprojects?archived=true', undefined],
    ['create', () => createAutoProject({ prompt: 'Do X', model: null }), 'POST', '/app/api/autoprojects', { prompt: 'Do X', model: null }],
    ['get', () => getAutoProject('ap1'), 'GET', '/app/api/autoprojects/ap1', undefined],
    ['rename', () => updateAutoProject('ap1', { title: 'T' }), 'PATCH', '/app/api/autoprojects/ap1', { title: 'T' }],
    ['archive', () => archiveAutoProject('ap1'), 'PUT', '/app/api/autoprojects/ap1/archive', undefined],
    ['unarchive', () => unarchiveAutoProject('ap1'), 'PUT', '/app/api/autoprojects/ap1/unarchive', undefined],
    ['read', () => markAutoProjectRead('ap1', 7), 'POST', '/app/api/autoprojects/ap1/read', { seq: 7 }],
    ['save brief', () => putAutoProjectBrief('ap1', '# B', 4), 'PUT', '/app/api/autoprojects/ap1/brief', { brief: '# B', expected_brief_version: 4 }],
    ['versions', () => listAutoProjectBriefVersions('ap1'), 'GET', '/app/api/autoprojects/ap1/brief/versions', undefined],
    ['version', () => getAutoProjectBriefVersion('ap1', 3), 'GET', '/app/api/autoprojects/ap1/brief/versions/3', undefined],
    ['restore', () => restoreAutoProjectBriefVersion('ap1', 3, 5), 'POST', '/app/api/autoprojects/ap1/brief/versions/3/restore', { expected_brief_version: 5 }],
    ['runs page 1', () => listAutoProjectRuns('ap1'), 'GET', '/app/api/autoprojects/ap1/runs', undefined],
    ['runs before', () => listAutoProjectRuns('ap1', { beforeSeq: 21, limit: 20 }), 'GET', '/app/api/autoprojects/ap1/runs?before_seq=21&limit=20', undefined],
    ['runs null before', () => listAutoProjectRuns('ap1', { beforeSeq: null }), 'GET', '/app/api/autoprojects/ap1/runs', undefined],
    ['start run', () => createAutoProjectRunConversation('ap1'), 'POST', '/app/api/autoprojects/ap1/runs', undefined],
    ['delete run', () => deleteAutoProjectRun('ap1', 'r 1'), 'DELETE', '/app/api/autoprojects/ap1/runs/r%201', undefined],
    ['answer', () => answerAutoProjectQuestion('ap1', 'h/1', 'yes'), 'POST', '/app/api/autoprojects/ap1/questions/h%2F1/answer', { answer: 'yes' }],
    ['encoded id', () => getAutoProject('a/b'), 'GET', '/app/api/autoprojects/a%2Fb', undefined],
  ] as const)('%s', async (_name, call, method, url, body) => {
    const fetchMock = stubFetch();
    await call();
    expect(fetchMock.last()).toEqual({ url, method, body, credentials: 'include' });
  });
});

describe('isAutoProjectDeleting', () => {
  it('marks an AutoProject from the start of its delete and keeps the mark after success', async () => {
    let resolve: (r: Response) => void = () => {};
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((r) => { resolve = r; })));
    expect(isAutoProjectDeleting('del-ok')).toBe(false);
    const pending = deleteAutoProject('del-ok');
    expect(isAutoProjectDeleting('del-ok')).toBe(true);
    resolve(jsonResponse(200, { success: true }));
    await expect(pending).resolves.toEqual({ success: true });
    expect(isAutoProjectDeleting('del-ok')).toBe(true);
    expect(isAutoProjectDeleting('other')).toBe(false);
  });

  it('clears the mark when the delete fails', async () => {
    stubFetch(() => jsonResponse(409, { detail: { error: 'autoproject_run_active', message: 'busy' } }));
    await expect(deleteAutoProject('del-fail')).rejects.toBeInstanceOf(ApiClientError);
    expect(isAutoProjectDeleting('del-fail')).toBe(false);
  });
});

describe('error accessors', () => {
  it('reads the current brief of a stale_brief', async () => {
    const err = await errorFrom(409, {
      detail: {
        error: 'stale_brief',
        message: 'm',
        current: { brief: 'Now', brief_version: 6, brief_updated_at: '2026-11-10T10:00:00Z' },
      },
    });
    expect(isStaleBriefError(err)).toBe(true);
    expect(staleBriefCurrent(err)).toEqual({ brief: 'Now', brief_version: 6, brief_updated_at: '2026-11-10T10:00:00Z' });
  });

  it.each([
    ['another code', { detail: { error: 'brief_too_long', current: { brief: 'x', brief_version: 1 } } }],
    ['no current', { detail: { error: 'stale_brief', message: 'm' } }],
    ['malformed current', { detail: { error: 'stale_brief', current: { brief: 1, brief_version: '2' } } }],
  ])('staleBriefCurrent is null for %s', async (_name, body) => {
    expect(staleBriefCurrent(await errorFrom(409, body))).toBeNull();
  });

  it('defaults a missing brief_updated_at to null', async () => {
    const err = await errorFrom(409, { detail: { error: 'stale_brief', current: { brief: '', brief_version: 0 } } });
    expect(staleBriefCurrent(err)).toEqual({ brief: '', brief_version: 0, brief_updated_at: null });
  });

  it('reads the active run of autoproject_run_active only', async () => {
    const active = { id: 'r2', seq: 2, status: 'waiting' };
    expect(autoprojectActiveRunFromError(
      await errorFrom(409, { detail: { error: 'autoproject_run_active', active_run: active } }),
    )).toEqual(active);
    expect(autoprojectActiveRunFromError(
      await errorFrom(409, { detail: { error: 'autoproject_archived', active_run: active } }),
    )).toBeNull();
    expect(autoprojectActiveRunFromError(new Error('x'))).toBeNull();
  });

  it.each([
    [{ id: 'r', seq: 1, status: 'running' }, { id: 'r', seq: 1, status: 'running' }],
    [{ id: 'r', seq: 1, status: 'completed' }, null],
    [{ id: 1, seq: 1, status: 'running' }, null],
    [{ id: 'r', seq: '1', status: 'running' }, null],
    [null, null],
    ['r', null],
  ])('parseAutoProjectActiveRun(%j)', (value, expected) => {
    expect(parseAutoProjectActiveRun(value)).toEqual(expected);
  });
});
