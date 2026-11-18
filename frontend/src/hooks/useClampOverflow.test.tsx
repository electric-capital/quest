// useClampOverflow: whether a clamped box's content is taller than the box
// (the "Show more" toggle), measured while clamped and kept while expanded.
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useClampOverflow } from './useClampOverflow';

/** A div whose scrollHeight / clientHeight the test sets. */
function box(scrollHeight: number, clientHeight: number) {
  const el = document.createElement('div');
  el.appendChild(document.createElement('p'));
  const size = { scrollHeight, clientHeight };
  Object.defineProperty(el, 'scrollHeight', { get: () => size.scrollHeight });
  Object.defineProperty(el, 'clientHeight', { get: () => size.clientHeight });
  return { el, size };
}

const observers: Array<{ callback: () => void; targets: Element[]; disconnected: boolean }> = [];

class FakeResizeObserver {
  private entry: { callback: () => void; targets: Element[]; disconnected: boolean };
  constructor(callback: () => void) {
    this.entry = { callback, targets: [], disconnected: false };
    observers.push(this.entry);
  }
  observe(target: Element) { this.entry.targets.push(target); }
  disconnect() { this.entry.disconnected = true; }
  unobserve() {}
}

beforeEach(() => {
  observers.length = 0;
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('useClampOverflow', () => {
  it.each([
    [300, 100, true],
    [101, 100, false], // within the 1px rounding slack
    [100, 100, false],
  ])('scrollHeight %i over clientHeight %i -> %s', (scroll, client, expected) => {
    const { el } = box(scroll, client);
    const { result } = renderHook(() => useClampOverflow({ current: el }, true, 'text', !expected));
    expect(result.current).toBe(expected);
  });

  it('re-measures on a resize of the box or its content', () => {
    const { el, size } = box(100, 100);
    const { result } = renderHook(() => useClampOverflow({ current: el }, true, 'text', false));
    expect(result.current).toBe(false);
    expect(observers[0].targets).toEqual([el, el.firstElementChild]);

    size.scrollHeight = 400;
    act(() => observers[0].callback());
    expect(result.current).toBe(true);
  });

  it('keeps the last answer while expanded and measures again when the content changes', () => {
    const { el, size } = box(400, 100);
    const ref = { current: el };
    const { result, rerender } = renderHook(
      ({ clamped, content }) => useClampOverflow(ref, clamped, content, false),
      { initialProps: { clamped: true, content: 'long' } },
    );
    expect(result.current).toBe(true);

    // Expanded: the box is as tall as its content, but the toggle stays.
    size.clientHeight = 400;
    rerender({ clamped: false, content: 'long' });
    expect(result.current).toBe(true);
    expect(observers[0].disconnected).toBe(true);

    size.clientHeight = 100;
    size.scrollHeight = 50;
    rerender({ clamped: true, content: 'short' });
    expect(result.current).toBe(false);
  });

  it('returns the initial answer with no element to measure', () => {
    const { result } = renderHook(() => useClampOverflow({ current: null }, true, 'text', true));
    expect(result.current).toBe(true);
  });
});
