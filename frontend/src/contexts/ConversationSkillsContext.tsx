/**
 * Per-conversation skill selection: the skills queued in a composer (selected
 * but not yet sent) and the skills already loaded into the conversation.
 */

import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';

export interface ConversationSkillsContextValue {
  getQueuedSkillsForConversation: (conversationId: string) => string[];
  setQueuedSkillsForConversation: (conversationId: string, skillIds: string[]) => void;
  getLoadedSkillsForConversation: (conversationId: string) => string[];
  setLoadedSkillsForConversation: (conversationId: string, skillIds: string[]) => void;
  // Merge the given IDs into the loaded set and clear the queued set.
  markSkillsAsLoaded: (conversationId: string, skillIds: string[]) => void;
}

const ConversationSkillsContext = createContext<ConversationSkillsContextValue | null>(null);

export function ConversationSkillsProvider({ children }: { children: React.ReactNode }) {
  const [conversationQueuedSkills, setConversationQueuedSkills] = useState<Record<string, string[]>>({});
  const [conversationLoadedSkills, setConversationLoadedSkills] = useState<Record<string, string[]>>({});

  const getQueuedSkillsForConversation = useCallback((conversationId: string): string[] => {
    return conversationQueuedSkills[conversationId] || [];
  }, [conversationQueuedSkills]);

  const setQueuedSkillsForConversation = useCallback((conversationId: string, skillIds: string[]) => {
    setConversationQueuedSkills((prev) => ({ ...prev, [conversationId]: skillIds }));
  }, []);

  const getLoadedSkillsForConversation = useCallback((conversationId: string): string[] => {
    return conversationLoadedSkills[conversationId] || [];
  }, [conversationLoadedSkills]);

  const setLoadedSkillsForConversation = useCallback((conversationId: string, skillIds: string[]) => {
    setConversationLoadedSkills((prev) => ({ ...prev, [conversationId]: skillIds }));
  }, []);

  const markSkillsAsLoaded = useCallback((conversationId: string, skillIds: string[]) => {
    // Merge the given IDs into the loaded set
    setConversationLoadedSkills((prev) => {
      const existing = prev[conversationId] || [];
      const existingSet = new Set(existing);
      const merged = [...existing, ...skillIds.filter((id) => !existingSet.has(id))];
      return { ...prev, [conversationId]: merged };
    });
    // Clear the queued set for this conversation
    setConversationQueuedSkills((prev) => {
      const next = { ...prev };
      delete next[conversationId];
      return next;
    });
  }, []);

  const value = useMemo<ConversationSkillsContextValue>(() => ({
    getQueuedSkillsForConversation,
    setQueuedSkillsForConversation,
    getLoadedSkillsForConversation,
    setLoadedSkillsForConversation,
    markSkillsAsLoaded,
  }), [
    getQueuedSkillsForConversation,
    setQueuedSkillsForConversation,
    getLoadedSkillsForConversation,
    setLoadedSkillsForConversation,
    markSkillsAsLoaded,
  ]);

  return <ConversationSkillsContext.Provider value={value}>{children}</ConversationSkillsContext.Provider>;
}

export function useConversationSkills(): ConversationSkillsContextValue {
  const context = useContext(ConversationSkillsContext);
  if (!context) {
    throw new Error('useConversationSkills must be used within a ConversationSkillsProvider');
  }
  return context;
}
