/**
 * The Sidebar's drill-down panel for one project: back/settings header, the
 * Routines section, and the project's conversation list with routine runs
 * folded into expandable groups. Data comes from useProjectConversations /
 * useProjectRoutines (owned by the Sidebar); this component renders it and
 * owns only its own row UI state.
 */

import { useMemo, useState } from 'react';
import type { Conversation, Project, Routine } from '../../api/types';
import { useConversationListActions } from '../../hooks/useConversationListActions';
import { useFlipListAnimation } from '../../hooks/useFlipListAnimation';
import { deriveProjectSidebarItems } from '../../utils/sidebarItems';
import { ConversationFilterMenu } from './ConversationFilterMenu';
import { ConversationRow } from './ConversationRow';
import { RoutinesSection } from './RoutinesSection';
import {
  ChevronLeftIcon,
  FolderIcon,
  GearIcon,
  GlobeIcon,
  PlusIcon,
  RoutineGroupChevronIcon,
  RoutineGroupIcon,
} from './icons';

interface ProjectPanelProps {
  project: Project;
  // The project's loaded conversations, unfiltered.
  conversations: Conversation[];
  loading: boolean;
  routines: Routine[];
  // Routines are hidden for public projects unless the
  // public_project_routines feature gate is open for this user.
  showRoutines: boolean;
  showArchived: boolean;
  onShowArchivedChange: (checked: boolean) => void;
  showSlack: boolean;
  onShowSlackChange: (checked: boolean) => void;
  expandedRoutineGroups: Record<string, boolean>;
  onToggleRoutineGroup: (routineId: string) => void;
  // null while another view (requests inbox) is showing, so no row is active.
  activeConversationId: string | null;
  // A conversation is being created in this project (new chat / routine run).
  busy: boolean;
  onBack: () => void;
  onOpenProjectSettings: () => void;
  onNewRoutine: () => void;
  onRoutineSettings: (routine: Routine) => void;
  onRunRoutine: (routine: Routine) => void;
  onNewChat: () => void;
  onSelect: (conversationId: string) => void;
  // Optimistic local edit of this project's rows (archive / rename).
  updateConversations: (updater: (prev: Conversation[]) => Conversation[]) => void;
  // A row was archived (the Sidebar deselects it if it was active).
  onArchived: (conversationId: string) => void;
}

export function ProjectPanel({
  project,
  conversations,
  loading,
  routines,
  showRoutines,
  showArchived,
  onShowArchivedChange,
  showSlack,
  onShowSlackChange,
  expandedRoutineGroups,
  onToggleRoutineGroup,
  activeConversationId,
  busy,
  onBack,
  onOpenProjectSettings,
  onNewRoutine,
  onRoutineSettings,
  onRunRoutine,
  onNewChat,
  onSelect,
  updateConversations,
  onArchived,
}: ProjectPanelProps) {
  const listRef = useFlipListAnimation<HTMLDivElement>();
  // Held here, not in the popover: toggling the archive filter reloads the
  // list and the header below is replaced by "Loading..." meanwhile.
  const [filterMenuOpen, setFilterMenuOpen] = useState(false);
  const actions = useConversationListActions({
    update: updateConversations,
    showArchived,
    onArchived,
  });

  const items = useMemo(
    () => deriveProjectSidebarItems(conversations, routines, { showArchived, showSlack }),
    [conversations, routines, showArchived, showSlack],
  );

  return (
    <>
      <div className="drill-down-header">
        <button className="drill-down-back-button" onClick={onBack}>
          <ChevronLeftIcon />
        </button>
        <FolderIcon className="drill-down-folder-icon" />
        <span className="drill-down-project-name">{project.name}</span>
        {project.public && (
          <span className="drill-down-public-badge" title="Public project — internet access, no internal data">
            <GlobeIcon />
            Public
          </span>
        )}
        <button
          className="drill-down-settings-button"
          onClick={onOpenProjectSettings}
          title="Project settings"
        >
          <GearIcon />
        </button>
      </div>

      <div className="drill-down-conversations">
        {loading ? (
          <div className="project-loading">Loading...</div>
        ) : (
          <>
            {showRoutines && (
              <RoutinesSection
                routines={routines}
                running={busy}
                onNewRoutine={onNewRoutine}
                onOpenSettings={onRoutineSettings}
                onRun={onRunRoutine}
              />
            )}

            {/* Conversations section header */}
            <div className="section-header">
              <div className="section-label">Conversations</div>
              <div className="section-header-actions">
                <button
                  className="section-add-button"
                  onClick={onNewChat}
                  disabled={busy}
                  title="New Chat"
                  aria-label="New Chat"
                >
                  <PlusIcon />
                </button>
                <ConversationFilterMenu
                  open={filterMenuOpen}
                  onOpenChange={setFilterMenuOpen}
                  options={[
                    { label: 'Show Archived', checked: showArchived, onChange: onShowArchivedChange },
                    { label: 'Show Slack Conversations', checked: showSlack, onChange: onShowSlackChange },
                  ]}
                />
              </div>
            </div>

            {/* Grouped conversations: routine groups interleaved with ungrouped conversations */}
            <div className="conversation-list" ref={listRef}>
              {items.map((item) => {
                if (item.type === 'routine-group') {
                  const group = item.group;
                  const isExpanded = expandedRoutineGroups[group.routineId] || false;
                  const hasActiveConvo = group.conversations.some(c => c.id === activeConversationId);

                  return (
                    <div
                      key={`routine-group-${group.routineId}`}
                      data-flip-id={`routine-group-${group.routineId}`}
                      className="routine-conversation-group"
                    >
                      <div
                        className={`routine-group-header ${hasActiveConvo && !isExpanded ? 'has-active' : ''}`}
                        onClick={() => onToggleRoutineGroup(group.routineId)}
                      >
                        <RoutineGroupChevronIcon expanded={isExpanded} />
                        <RoutineGroupIcon />
                        <span className="routine-group-name">{group.routineName}</span>
                        <span className="routine-group-count">{group.conversations.length}</span>
                      </div>
                      {isExpanded && (
                        <div className="routine-group-items">
                          {group.conversations.map((conversation) => (
                            <ConversationRow
                              key={conversation.id}
                              conversation={conversation}
                              variant="routine-run"
                              active={activeConversationId === conversation.id}
                              actions={actions}
                              onSelect={() => onSelect(conversation.id)}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  );
                }
                const conversation = item.conversation;
                return (
                  <ConversationRow
                    key={conversation.id}
                    conversation={conversation}
                    active={activeConversationId === conversation.id}
                    actions={actions}
                    onSelect={() => onSelect(conversation.id)}
                  />
                );
              })}
            </div>

            {conversations.length === 0 && (
              <div className="sidebar-empty">No conversations yet</div>
            )}
          </>
        )}
      </div>
    </>
  );
}
