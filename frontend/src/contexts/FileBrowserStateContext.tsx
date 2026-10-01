/**
 * Per-conversation file-browser navigation state (current path + history),
 * kept across conversation switches so returning to a chat restores where
 * the user was browsing. Changes on every folder click, which is why it is
 * its own context: nothing else should re-render for it.
 */

import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';

export interface FileBrowserState {
  path: string;
  history: string[];
  historyIndex: number;
}

const INITIAL_FILE_BROWSER_STATE: FileBrowserState = { path: '/', history: ['/'], historyIndex: 0 };

export interface FileBrowserStateContextValue {
  getFileBrowserState: (conversationId: string) => FileBrowserState;
  setFileBrowserState: (conversationId: string, state: FileBrowserState) => void;
}

const FileBrowserStateContext = createContext<FileBrowserStateContextValue | null>(null);

export function FileBrowserStateProvider({ children }: { children: React.ReactNode }) {
  const [fileBrowserStates, setFileBrowserStates] = useState<Record<string, FileBrowserState>>({});

  const getFileBrowserState = useCallback((conversationId: string): FileBrowserState => {
    return fileBrowserStates[conversationId] || INITIAL_FILE_BROWSER_STATE;
  }, [fileBrowserStates]);

  const setFileBrowserState = useCallback((conversationId: string, state: FileBrowserState) => {
    setFileBrowserStates((prev) => ({ ...prev, [conversationId]: state }));
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
