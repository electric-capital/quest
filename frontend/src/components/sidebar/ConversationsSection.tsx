/**
 * The Sidebar's top-level "Conversations" section: header with New Chat +
 * filter popover, the paged standalone conversation list, and the
 * auto-paging "Load more" row. Data comes from useTopLevelConversations;
 * this component only renders it.
 *
 * Renders a fragment (header + list as siblings) because the stylesheet
 * targets `.sidebar-panel-main > .section-header`.
 */

import { useEffect, useRef, useState } from 'react';
import type { Conversation } from '../../api/types';
import { useConversationListActions } from '../../hooks/useConversationListActions';
import { useFlipListAnimation } from '../../hooks/useFlipListAnimation';
import type { TopLevelConversations } from '../../hooks/useTopLevelConversations';
import { ConversationFilterMenu } from './ConversationFilterMenu';
import { ConversationRow } from './ConversationRow';
import { PlusIcon } from './icons';

interface ConversationsSectionProps {
  list: TopLevelConversations;
  // null while another view (requests inbox) is showing, so no row is active.
  activeConversationId: string | null;
  // A New Chat / Duplicate Workspace is in flight.
  creating: boolean;
  onSelect: (conversationId: string) => void;
  onNewChat: () => void;
  onConvertToProject: (conversation: Conversation) => void;
  onDuplicateWorkspace: (conversationId: string) => void;
  // A row was archived (the Sidebar deselects it if it was active).
  onArchived: (conversationId: string) => void;
}

const CONVERTIBLE_ORIGINS_EXCLUDED = new Set(['slack', 'user_subagent', 'inference_api']);

export function ConversationsSection({
  list,
  activeConversationId,
  creating,
  onSelect,
  onNewChat,
  onConvertToProject,
  onDuplicateWorkspace,
  onArchived,
}: ConversationsSectionProps) {
  // Glide rows to their new slot when a background refresh re-sorts the
  // list (a conversation with new activity jumps to the top).
  const listRef = useFlipListAnimation<HTMLDivElement>();
  // Held here, not in the popover: a filter toggle reloads the list and the
  // header below is replaced by the loading placeholder meanwhile.
  const [filterMenuOpen, setFilterMenuOpen] = useState(false);
  const actions = useConversationListActions({
    update: list.updateConversations,
    showArchived: list.filters.archived,
    onArchived,
  });

  // Auto-page as the user scrolls: when the "Load more" row scrolls into
  // view (or is still in view after a page lands), fetch the next page.
  // The button stays clickable as a manual fallback. `list.loadMore`
  // changes identity after every page (cursor / in-flight flag), so the
  // observer is rebuilt and re-reports the row's current visibility --
  // IntersectionObserver only fires on *changes*, and a fresh observe() is
  // what keeps paging while the row stays on screen.
  const loadMoreButtonRef = useRef<HTMLButtonElement | null>(null);
  const { hasMore, loadMore } = list;
  useEffect(() => {
    const el = loadMoreButtonRef.current;
    if (!hasMore || !el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          loadMore();
        }
      },
      // Start fetching a bit before the row actually enters the viewport
      // so fast scrolling doesn't stall on the pager.
      { rootMargin: '150px' }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [hasMore, loadMore]);

  if (list.loading) {
    return (
      <div className="sidebar-loading">
        Loading conversations...
      </div>
    );
  }

  return (
    <>
      <div className="section-header">
        <div className="section-label">Conversations</div>
        <div className="section-header-actions">
          <button
            className="section-add-button"
            onClick={onNewChat}
            disabled={creating}
            title="New Chat"
            aria-label="New Chat"
          >
            <PlusIcon />
          </button>
          <ConversationFilterMenu
            open={filterMenuOpen}
            onOpenChange={setFilterMenuOpen}
            options={[
              {
                label: 'Show Archived',
                checked: list.filters.archived,
                onChange: (checked) => list.setFilter('archived', checked),
              },
              {
                label: 'Show Slack Conversations',
                checked: list.filters.slack,
                onChange: (checked) => list.setFilter('slack', checked),
              },
              {
                label: 'Show Inference API Runs',
                checked: list.filters.inference,
                onChange: (checked) => list.setFilter('inference', checked),
              },
            ]}
          />
        </div>
      </div>
      <div className="conversation-list" ref={listRef}>
        {list.conversations.map((conversation) => (
          <ConversationRow
            key={conversation.id}
            conversation={conversation}
            active={activeConversationId === conversation.id}
            actions={actions}
            onSelect={() => onSelect(conversation.id)}
            menuExtras={
              <>
                {!CONVERTIBLE_ORIGINS_EXCLUDED.has(conversation.origin ?? '') && (
                  <button
                    className="conversation-menu-item"
                    onClick={(e) => {
                      e.stopPropagation();
                      actions.closeMenu();
                      onConvertToProject(conversation);
                    }}
                  >
                    Create Project from Chat
                  </button>
                )}
                <button
                  className="conversation-menu-item"
                  onClick={(e) => {
                    e.stopPropagation();
                    actions.closeMenu();
                    onDuplicateWorkspace(conversation.id);
                  }}
                >
                  Duplicate Workspace
                </button>
              </>
            }
          />
        ))}
        {list.hasMore && (
          <button
            ref={loadMoreButtonRef}
            className="load-more-conversations-button"
            onClick={list.loadMore}
            disabled={list.loadingMore}
          >
            {list.loadingMore ? 'Loading...' : 'Load more'}
          </button>
        )}
      </div>
    </>
  );
}
