/**
 * Per-row UI state and optimistic mutations for one Sidebar conversation
 * list: the single open "more options" menu, the inline rename editor, and
 * archive / unarchive. The list itself is owned by a data hook; this hook
 * only needs its `update` function (plus the list's archive toggle, which
 * decides whether an archived row is flagged in place or dropped).
 */

import { useCallback, useEffect, useState } from 'react';
import { archiveConversation, renameConversation, unarchiveConversation } from '../api/client';
import type { Conversation } from '../api/types';

export interface ConversationListActions {
  openMenuId: string | null;
  toggleMenu: (conversationId: string) => void;
  closeMenu: () => void;
  renamingId: string | null;
  renameValue: string;
  setRenameValue: (value: string) => void;
  startRename: (conversationId: string, currentName: string) => void;
  submitRename: (conversationId: string) => void;
  cancelRename: () => void;
  archive: (conversationId: string) => void;
  unarchive: (conversationId: string) => void;
}

interface Options {
  update: (updater: (prev: Conversation[]) => Conversation[]) => void;
  showArchived: boolean;
  // Called after a row is archived (the Sidebar deselects it if active).
  onArchived?: (conversationId: string) => void;
}

export function useConversationListActions({ update, showArchived, onArchived }: Options): ConversationListActions {
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState<string>('');

  // Close the open menu on click outside
  useEffect(() => {
    if (!openMenuId) return;
    const handleClickOutside = () => setOpenMenuId(null);
    document.addEventListener('click', handleClickOutside);
    return () => document.removeEventListener('click', handleClickOutside);
  }, [openMenuId]);

  const toggleMenu = useCallback((conversationId: string) => {
    setOpenMenuId((prev) => (prev === conversationId ? null : conversationId));
  }, []);

  const closeMenu = useCallback(() => setOpenMenuId(null), []);

  const startRename = useCallback((conversationId: string, currentName: string) => {
    setOpenMenuId(null);
    setRenamingId(conversationId);
    // Pre-fill with empty string for placeholder titles like "New Chat"
    setRenameValue(currentName === 'New Chat' ? '' : currentName);
  }, []);

  const submitRename = useCallback((conversationId: string) => {
    const trimmed = renameValue.trim();
    const newName = trimmed || null; // empty string = clear custom name

    // Optimistic UI update
    update((prev) =>
      prev.map((c) =>
        c.id === conversationId
          ? { ...c, custom_name: newName, title: newName || c.title }
          : c
      )
    );

    setRenamingId(null);
    setRenameValue('');

    // Fire-and-forget API call
    renameConversation(conversationId, newName).catch((err) => {
      console.error('Failed to rename conversation:', err);
    });
  }, [renameValue, update]);

  const cancelRename = useCallback(() => {
    setRenamingId(null);
    setRenameValue('');
  }, []);

  const archive = useCallback((conversationId: string) => {
    setOpenMenuId(null);

    // Optimistic local state update (avoids full reload flicker)
    update((prev) => {
      if (showArchived) {
        // Toggle flag in-place so the item stays visible but shows as archived
        return prev.map((c) => (c.id === conversationId ? { ...c, archived: true } : c));
      }
      // Remove from list when archived items are hidden
      return prev.filter((c) => c.id !== conversationId);
    });

    onArchived?.(conversationId);

    // Fire-and-forget API call to persist on the server
    archiveConversation(conversationId).catch((err) => {
      console.error('Failed to archive conversation:', err);
    });
  }, [update, showArchived, onArchived]);

  const unarchive = useCallback((conversationId: string) => {
    setOpenMenuId(null);

    // Optimistic local state update
    update((prev) => prev.map((c) => (c.id === conversationId ? { ...c, archived: false } : c)));

    // Fire-and-forget API call to persist on the server
    unarchiveConversation(conversationId).catch((err) => {
      console.error('Failed to unarchive conversation:', err);
    });
  }, [update]);

  return {
    openMenuId,
    toggleMenu,
    closeMenu,
    renamingId,
    renameValue,
    setRenameValue,
    startRename,
    submitRename,
    cancelRename,
    archive,
    unarchive,
  };
}
