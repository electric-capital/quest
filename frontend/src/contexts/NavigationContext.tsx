/**
 * View state: which conversation / view is showing, and the one-shot
 * hand-offs between views (home composer -> ChatPanel first send, Sidebar
 * routine run -> ChatPanel auto-send, search result -> scroll target).
 *
 * The URL is the source of truth for the active conversation and project;
 * App.tsx mirrors its params into `activeConversationId` here (and into
 * ProjectsContext.activeProjectId) so components off the route tree stay in
 * sync.
 */

import React, { createContext, useContext, useMemo, useState } from 'react';
import type { ComposerAttachmentRef } from '../api/types';

// Pending routine message (set by Sidebar when a routine is invoked, consumed by ChatPanel)
export interface PendingRoutineMessage {
  conversationId: string;
  prompt: string;
  guideId: string | null;
}

// Pending first message (set by the root HomeComposer, consumed by ChatPanel).
// Carries the home-chosen model/skills/flags into the first send. Distinct
// from PendingRoutineMessage because it must carry model + skillIds + flags.
export interface PendingFirstMessage {
  conversationId: string;
  prompt: string;
  model: string;
  skillIds: string[];
  flags: string[];
  // Whether the chat was started inside a public project (the home
  // composer knows from the drilled project; ChatPanel only learns it
  // asynchronously), so the first send persists the right last-used pick.
  isPublicProject: boolean;
  // Workspace-relative names of generic files attached on the home screen,
  // uploaded into the new workspace before navigation; forwarded into the
  // first send so they appear in the triggering turn's metadata. Empty when
  // nothing was attached.
  attachedFilenames: string[];
  // Refs of clipboard images pasted on the home screen, uploaded via the
  // composer-attachments endpoint after the conversation was created;
  // forwarded as the first send's multimodal attachments. Empty when none.
  attachments: ComposerAttachmentRef[];
}

export interface NavigationContextValue {
  activeConversationId: string | null;
  setActiveConversationId: (id: string | null) => void;
  // Requests view state
  showRequestsView: boolean;
  setShowRequestsView: (show: boolean) => void;
  // Scroll-to-message state (used by search to scroll to a specific message)
  scrollToMessageIndex: number | null;
  setScrollToMessageIndex: (index: number | null) => void;
  // Settings modal
  isSettingsOpen: boolean;
  setSettingsOpen: (open: boolean) => void;
  settingsInitialSection: string | null;
  setSettingsInitialSection: (section: string | null) => void;
  // Pending routine message (set when a routine is invoked, consumed by ChatPanel)
  pendingRoutineMessage: PendingRoutineMessage | null;
  setPendingRoutineMessage: (msg: PendingRoutineMessage | null) => void;
  // Pending first message (set by the root HomeComposer, consumed by ChatPanel)
  pendingFirstMessage: PendingFirstMessage | null;
  setPendingFirstMessage: (msg: PendingFirstMessage | null) => void;
}

const NavigationContext = createContext<NavigationContextValue | null>(null);

export function NavigationProvider({ children }: { children: React.ReactNode }) {
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [showRequestsView, setShowRequestsView] = useState(false);
  const [scrollToMessageIndex, setScrollToMessageIndex] = useState<number | null>(null);
  const [isSettingsOpen, setSettingsOpen] = useState(false);
  const [settingsInitialSection, setSettingsInitialSection] = useState<string | null>(null);
  const [pendingRoutineMessage, setPendingRoutineMessage] = useState<PendingRoutineMessage | null>(null);
  const [pendingFirstMessage, setPendingFirstMessage] = useState<PendingFirstMessage | null>(null);

  const value = useMemo<NavigationContextValue>(() => ({
    activeConversationId,
    setActiveConversationId,
    showRequestsView,
    setShowRequestsView,
    scrollToMessageIndex,
    setScrollToMessageIndex,
    isSettingsOpen,
    setSettingsOpen,
    settingsInitialSection,
    setSettingsInitialSection,
    pendingRoutineMessage,
    setPendingRoutineMessage,
    pendingFirstMessage,
    setPendingFirstMessage,
  }), [
    activeConversationId,
    showRequestsView,
    scrollToMessageIndex,
    isSettingsOpen,
    settingsInitialSection,
    pendingRoutineMessage,
    pendingFirstMessage,
  ]);

  return <NavigationContext.Provider value={value}>{children}</NavigationContext.Provider>;
}

export function useNavigationState(): NavigationContextValue {
  const context = useContext(NavigationContext);
  if (!context) {
    throw new Error('useNavigationState must be used within a NavigationProvider');
  }
  return context;
}
