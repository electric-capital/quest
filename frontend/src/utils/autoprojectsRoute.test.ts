// AutoProjects URLs: the path builders and the parser that decides which
// AutoProjects view shows (list, archived list, new, page, run), refusing
// anything that is not id-shaped.
import { describe, expect, it } from 'vitest';
import {
  autoprojectPath,
  autoprojectRunPath,
  autoprojectsListPath,
  newAutoProjectPath,
  parseAutoProjectsRoute,
} from './autoprojectsRoute';

describe('path builders', () => {
  it('builds every AutoProjects URL', () => {
    expect(autoprojectsListPath()).toBe('/autoprojects');
    expect(autoprojectsListPath({ archived: false })).toBe('/autoprojects');
    expect(autoprojectsListPath({ archived: true })).toBe('/autoprojects?archived=true');
    expect(newAutoProjectPath()).toBe('/autoprojects/new');
    expect(autoprojectPath('ap-1')).toBe('/autoprojects/ap-1');
    expect(autoprojectRunPath('ap-1', 'conv_2')).toBe('/autoprojects/ap-1/runs/conv_2');
  });

  it('encodes ids', () => {
    expect(autoprojectPath('a/b')).toBe('/autoprojects/a%2Fb');
    expect(autoprojectRunPath('a', 'c d')).toBe('/autoprojects/a/runs/c%20d');
  });

  it('round-trips through the parser', () => {
    expect(parseAutoProjectsRoute(autoprojectPath('ap1'), '')).toEqual({ kind: 'page', autoprojectId: 'ap1' });
    expect(parseAutoProjectsRoute(autoprojectRunPath('ap1', 'c1'), '')).toEqual({
      kind: 'run', autoprojectId: 'ap1', conversationId: 'c1',
    });
    const [path, query] = autoprojectsListPath({ archived: true }).split('?');
    expect(parseAutoProjectsRoute(path, query)).toEqual({ kind: 'list', archived: true });
  });
});

describe('parseAutoProjectsRoute', () => {
  it.each([
    ['/autoprojects', '', { kind: 'list', archived: false }],
    ['/autoprojects/', '', { kind: 'list', archived: false }],
    ['/autoprojects', '?archived=true', { kind: 'list', archived: true }],
    ['/autoprojects', 'archived=true', { kind: 'list', archived: true }],
    ['/autoprojects', '?archived=1', { kind: 'list', archived: false }],
    ['/autoprojects/new', '', { kind: 'new' }],
    ['/autoprojects/new/', '', { kind: 'new' }],
    ['/autoprojects/2f1c-uuid', '', { kind: 'page', autoprojectId: '2f1c-uuid' }],
    ['/autoprojects/2f1c-uuid/', '?archived=true', { kind: 'page', autoprojectId: '2f1c-uuid' }],
    ['/autoprojects/ap1/runs/c1', '', { kind: 'run', autoprojectId: 'ap1', conversationId: 'c1' }],
    ['/autoprojects/ap1/runs/c1/', '', { kind: 'run', autoprojectId: 'ap1', conversationId: 'c1' }],
  ])('%s%s', (pathname, search, expected) => {
    expect(parseAutoProjectsRoute(pathname, search)).toEqual(expected);
  });

  it.each([
    ['/'],
    ['/projects/p1'],
    ['/autoprojectsx'],
    ['/autoprojects//'],
    ['/autoprojects/new/runs/c1'],
    ['/autoprojects/ap1/runs'],
    ['/autoprojects/ap1/runs/'],
    ['/autoprojects/ap1/files/c1'],
    ['/autoprojects/ap1/runs/c1/extra'],
    ['/autoprojects/a%2Fb'],
    ['/autoprojects/..'],
    ['/autoprojects/%2E%2E'],
    ['/autoprojects/ap1/runs/%2E%2E'],
    ['/autoprojects/bad%zz'],
    ['/autoprojects/has space'],
    [`/autoprojects/${'a'.repeat(65)}`],
  ])('is not an AutoProjects route: %s', (pathname) => {
    expect(parseAutoProjectsRoute(pathname, '')).toBeNull();
  });

  it('accepts an id of the maximum length', () => {
    const id = 'a'.repeat(64);
    expect(parseAutoProjectsRoute(`/autoprojects/${id}`, '')).toEqual({ kind: 'page', autoprojectId: id });
  });
});
