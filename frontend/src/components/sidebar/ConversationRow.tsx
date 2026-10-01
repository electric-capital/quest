/**
 * One conversation row in a Sidebar list: origin badge + title (or the
 * inline rename editor), the "more options" button and its dropdown.
 *
 * Shared by the top-level list, the drilled project's standalone rows and
 * the rows inside a routine-run group; the `variant` picks the routine-run
 * presentation (timestamp label, no FLIP id). Row UI state and the
 * archive / rename mutations come from useConversationListActions.
 */

import type React from 'react';
import type { Conversation } from '../../api/types';
import type { ConversationListActions } from '../../hooks/useConversationListActions';
import { formatRoutineTimestamp } from '../../utils/sidebarItems';
import { ConversationOriginIcon, KebabIcon } from './icons';

export interface ConversationRowProps {
  conversation: Conversation;
  active: boolean;
  // 'routine-run' rows (inside a routine group) show when the run started
  // instead of the title, since every run of a routine shares its name.
  variant?: 'default' | 'routine-run';
  actions: ConversationListActions;
  onSelect: () => void;
  // Extra dropdown items rendered between Rename and Archive (the top-level
  // list adds "Create Project from Chat" and "Duplicate Workspace").
  menuExtras?: React.ReactNode;
}

export function ConversationRow({
  conversation,
  active,
  variant = 'default',
  actions,
  onSelect,
  menuExtras,
}: ConversationRowProps) {
  const isRoutineRun = variant === 'routine-run';
  const title = conversation.custom_name || conversation.title;
  const label = isRoutineRun
    ? (conversation.custom_name
        ? `${conversation.custom_name} (${formatRoutineTimestamp(conversation.created_at)})`
        : formatRoutineTimestamp(conversation.created_at))
    : title;
  // Routine runs rename from their custom name only (the placeholder is the
  // timestamp, not a title worth editing).
  const renamePrefill = isRoutineRun ? (conversation.custom_name || '') : title;
  const classes = [
    'conversation-item',
    isRoutineRun ? 'routine-sub-item' : '',
    active ? 'active' : '',
    conversation.archived ? 'archived' : '',
  ].filter(Boolean).join(' ');

  return (
    <div
      data-flip-id={isRoutineRun ? undefined : conversation.id}
      className={classes}
      onClick={onSelect}
    >
      {actions.renamingId === conversation.id ? (
        <input
          className="conversation-rename-input"
          type="text"
          value={actions.renameValue}
          onChange={(e) => actions.setRenameValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              actions.submitRename(conversation.id);
            } else if (e.key === 'Escape') {
              actions.cancelRename();
            }
          }}
          onBlur={() => actions.submitRename(conversation.id)}
          autoFocus
          onFocus={(e) => e.target.select()}
          maxLength={100}
          onClick={(e) => e.stopPropagation()}
        />
      ) : (
        <div className={isRoutineRun ? 'conversation-title routine-sub-item-timestamp' : 'conversation-title'}>
          <ConversationOriginIcon origin={conversation.origin} />
          {label}
        </div>
      )}
      <button
        className="conversation-menu-button"
        onClick={(e) => {
          e.stopPropagation();
          actions.toggleMenu(conversation.id);
        }}
        title="More options"
      >
        <KebabIcon />
      </button>
      {actions.openMenuId === conversation.id && (
        <div className="conversation-menu-dropdown">
          <button
            className="conversation-menu-item"
            onClick={(e) => {
              e.stopPropagation();
              actions.startRename(conversation.id, renamePrefill);
            }}
          >
            Rename
          </button>
          {menuExtras}
          {conversation.archived ? (
            <button
              className="conversation-menu-item"
              onClick={(e) => {
                e.stopPropagation();
                actions.unarchive(conversation.id);
              }}
            >
              Unarchive
            </button>
          ) : (
            <button
              className="conversation-menu-item"
              onClick={(e) => {
                e.stopPropagation();
                actions.archive(conversation.id);
              }}
            >
              Archive
            </button>
          )}
        </div>
      )}
    </div>
  );
}
