/**
 * Loader for the code-highlighting and math chunk (components/codeAndMath.ts:
 * highlight.js and KaTeX). The chunk is fetched the first time a component
 * asks for it with `useCodeAndMath(true)` -- RichMarkdown for markdown with a
 * code fence or a `$`, ToolUseMessage for a run_python script -- and every
 * component that wanted it re-renders when it lands. Until then they render
 * without it (plain code, math as its source). Concurrent requests share
 * one fetch; a failed fetch is logged and retried by the next request.
 */
import { useEffect, useSyncExternalStore } from 'react';

export type CodeAndMath = typeof import('../components/codeAndMath');

let loaded: CodeAndMath | null = null;
let pending: Promise<void> | null = null;
const listeners = new Set<() => void>();

/** Fetch the chunk (once); resolves when it is in, or after a logged failure. */
export function loadCodeAndMath(): Promise<void> {
  if (loaded) return Promise.resolve();
  pending ??= import('../components/codeAndMath').then(
    (chunk) => {
      loaded = chunk;
      listeners.forEach((listener) => listener());
    },
    (error: unknown) => {
      pending = null;
      console.error('Code highlighting and math rendering failed to load:', error);
    },
  );
  return pending;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getLoaded(): CodeAndMath | null {
  return loaded;
}

function getNothing(): null {
  return null;
}

/**
 * The chunk's exports when `wanted`, fetching it on first need; null until it
 * lands. With `wanted` false it returns null and never re-renders the caller.
 */
export function useCodeAndMath(wanted: boolean): CodeAndMath | null {
  const chunk = useSyncExternalStore(subscribe, wanted ? getLoaded : getNothing);
  const missing = wanted && chunk === null;
  useEffect(() => {
    if (missing) void loadCodeAndMath();
  }, [missing]);
  return chunk;
}

/** True once the chunk is in; re-renders the caller when it lands. */
export function useCodeAndMathLoaded(): boolean {
  return useSyncExternalStore(subscribe, getLoaded) !== null;
}
