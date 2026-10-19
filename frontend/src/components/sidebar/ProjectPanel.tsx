/**
 * The Sidebar's drill-down panel for one project: back/settings header, the
 * Routines section, the project's Docs block (Quest Docs gate open only), its
 * Tables block (Quest Tables gate open only), and the project's conversation
 * list with routine runs folded into expandable groups. Data comes from
 * useProjectConversations / useProjectRoutines / useDocs / useTables (owned
 * by the Sidebar); this component renders it and owns only
 * its own row UI state.
 */

import { useMemo, useState } from 'react';
import { Archive } from 'lucide-react';
import type { Conversation, Doc, Project, Routine, TableListRow } from '../../api/types';
import { useConversationListActions } from '../../hooks/useConversationListActions';
import { useFlipListAnimation } from '../../hooks/useFlipListAnimation';
import { deriveProjectSidebarItems } from '../../utils/sidebarItems';
import { ConversationFilterMenu } from './ConversationFilterMenu';
import { ConversationRow } from './ConversationRow';
import { DocsSection } from './DocsSection';
import { RoutinesSection } from './RoutinesSection';
import { TablesSection } from './TablesSection';
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
  // The project's Docs block, rendered between Routines and Conversations
  // while `showDocs` (the Quest Docs feature gate is open for the user).
  showDocs?: boolean;
  docs?: Doc[];
  docsLoading?: boolean;
  // The project's first docs page failed to load.
  docsError?: string | null;
  // The doc open in the viewer (its row is highlighted).
  activeDocId?: string | null;
  onOpenDoc?: (id: string) => void;
  // Opens the All Docs view filtered to this project.
  onOpenAllDocs?: () => void;
  // That filtered All Docs view is the one showing.
  docsHeaderActive?: boolean;
  // The project's Tables block, rendered right under the Docs block while
  // `showTables` (the Quest Tables feature gate is open for the user).
  showTables?: boolean;
  tables?: TableListRow[];
  tablesLoading?: boolean;
  // The project's first tables page failed to load.
  tablesError?: string | null;
  // The table open in the viewer (its row is highlighted).
  activeTableId?: string | null;
  onOpenTable?: (id: string) => void;
  // Opens the All Tables view filtered to this project.
  onOpenAllTables?: () => void;
  // That filtered All Tables view is the one showing.
  tablesHeaderActive?: boolean;
}

const NO_DOCS: Doc[] = [];
const NO_TABLES: TableListRow[] = [];
const noop = () => {};

export function ProjectPanel({
  project,
  conversations,
  loading,
  routines,
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
  showDocs = false,
  docs = NO_DOCS,
  docsLoading = false,
  docsError = null,
  activeDocId = null,
  onOpenDoc = noop,
  onOpenAllDocs = noop,
  docsHeaderActive = false,
  showTables = false,
  tables = NO_TABLES,
  tablesLoading = false,
  tablesError = null,
  activeTableId = null,
  onOpenTable = noop,
  onOpenAllTables = noop,
  tablesHeaderActive = false,
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
        {project.archived && (
          <span
            className="drill-down-archived-badge"
            title="Archived project — hidden from the projects list, scheduled routines paused"
          >
            <Archive size={12} />
            Archived
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
            <RoutinesSection
              routines={routines}
              running={busy}
              onNewRoutine={onNewRoutine}
              onOpenSettings={onRoutineSettings}
              onRun={onRunRoutine}
            />

            {showDocs && (
              <DocsSection
                docs={docs}
                loading={docsLoading}
                error={docsError}
                activeDocId={activeDocId}
                onOpenDoc={onOpenDoc}
                onOpenAll={onOpenAllDocs}
                headerActive={docsHeaderActive}
              />
            )}

            {showTables && (
              <TablesSection
                tables={tables}
                loading={tablesLoading}
                error={tablesError}
                activeTableId={activeTableId}
                onOpenTable={onOpenTable}
                onOpenAll={onOpenAllTables}
                headerActive={tablesHeaderActive}
                publicProject={Boolean(project.public)}
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
