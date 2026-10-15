// LazyBoundary + lazyNamed: the Suspense fallback until a chunk loads, the
// reload notice (optionally framed) when it fails, and other render errors
// passing through to the boundary above.
import { Component, type ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { LazyBoundary, lazyNamed } from './LazyBoundary';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function Hello({ name }: { name: string }) {
  return <p>Hello {name}</p>;
}

type HelloChunk = { Hello: typeof Hello };

/** Records what reaches the boundary above LazyBoundary. */
class OuterBoundary extends Component<{ children: ReactNode }, { error: unknown }> {
  state: { error: unknown } = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error };
  }

  render() {
    const { error } = this.state;
    if (error) return <p>Outer caught: {error instanceof Error ? error.message : String(error)}</p>;
    return this.props.children;
  }
}

describe('LazyBoundary', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('shows the fallback until the chunk loads, then the named export', async () => {
    const chunk = deferred<HelloChunk>();
    const LazyHello = lazyNamed(() => chunk.promise, 'Hello');
    render(
      <LazyBoundary fallback={<p>Loading...</p>}>
        <LazyHello name="Ada" />
      </LazyBoundary>,
    );
    expect(screen.getByText('Loading...')).toBeTruthy();

    await act(async () => chunk.resolve({ Hello }));
    expect(screen.getByText('Hello Ada')).toBeTruthy();
    expect(screen.queryByText('Loading...')).toBeNull();
  });

  it('turns a failed chunk load into a reload notice', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const chunk = deferred<HelloChunk>();
    const LazyHello = lazyNamed(() => chunk.promise, 'Hello');
    render(
      <OuterBoundary>
        <LazyBoundary fallback={<p>Loading...</p>}>
          <LazyHello name="Ada" />
        </LazyBoundary>
      </OuterBoundary>,
    );

    await act(async () => chunk.reject(new TypeError('Failed to fetch dynamically imported module')));
    const notice = screen.getByRole('alert');
    expect(notice.textContent).toContain('This part of the app could not be loaded.');
    expect(screen.getByRole('button', { name: 'Reload page' })).toBeTruthy();
    expect(screen.queryByText(/Outer caught/)).toBeNull();
  });

  it('wraps the notice in `frame` when given', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const LazyHello = lazyNamed(
      () => Promise.reject<HelloChunk>(new Error('404')),
      'Hello',
    );
    render(
      <LazyBoundary fallback={null} frame={(notice) => <section aria-label="Framed">{notice}</section>}>
        <LazyHello name="Ada" />
      </LazyBoundary>,
    );
    const framed = await screen.findByRole('region', { name: 'Framed' });
    expect(framed.querySelector('[role="alert"]')).not.toBeNull();
  });

  it('passes other render errors to the boundary above', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    function Broken(): ReactNode {
      throw new Error('render bug');
    }
    render(
      <OuterBoundary>
        <LazyBoundary fallback={null}>
          <Broken />
        </LazyBoundary>
      </OuterBoundary>,
    );
    expect(screen.getByText('Outer caught: render bug')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
