/**
 * Per-file-space file-browser state (current path + history + the dotfile
 * toggle), kept across conversation switches so returning to a chat restores
 * where the user was browsing. Keyed by `fileSourceKey()` --
 * `conversation:<id>` / `project:<id>` -- so a Chat Files card and a Project
 * Files card shown side by side keep separate paths and toggles. Changes on
 * every folder click, which is why it is its own context: nothing else
 * should re-render for it.
 */

import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';

export interface FileBrowserState {
  path: string;
  history: string[];
  historyIndex: number;
  /** Dot-prefixed entries revealed (the toolbar "Show hidden files" toggle). */
  showHidden: boolean;
}

export const INITIAL_FILE_BROWSER_STATE: FileBrowserState = {
  path: '/',
  history: ['/'],
  historyIndex: 0,
  showHidden: false,
};

export interface FileBrowserStateContextValue {
  /** State for one file space; `key` is `fileSourceKey(source)`. */
  getFileBrowserState: (key: string) => FileBrowserState;
  setFileBrowserState: (key: string, state: FileBrowserState) => void;
}

const FileBrowserStateContext = createContext<FileBrowserStateContextValue | null>(null);

export function FileBrowserStateProvider({ children }: { children: React.ReactNode }) {
  const [fileBrowserStates, setFileBrowserStates] = useState<Record<string, FileBrowserState>>({});

  const getFileBrowserState = useCallback((key: string): FileBrowserState => {
    return fileBrowserStates[key] || INITIAL_FILE_BROWSER_STATE;
  }, [fileBrowserStates]);

  const setFileBrowserState = useCallback((key: string, state: FileBrowserState) => {
    setFileBrowserStates((prev) => ({ ...prev, [key]: state }));
  }, []);

  const value = useMemo<FileBrowserStateContextValue>(
    () => ({ getFileBrowserState, setFileBrowserState }),
    [getFileBrowserState, setFileBrowserState],
  );

  return <FileBrowserStateContext.Provider value={value}>{children}</FileBrowserStateContext.Provider>;
}

export function useFileBrowserState(): FileBrowserStateContextValue {
  const context = useContext(FileBrowserStateContext);
  if (!context) {
    throw new Error('useFileBrowserState must be used within a FileBrowserStateProvider');
  }
  return context;
}
