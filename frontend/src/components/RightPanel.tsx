/**
 * Right panel wrapper hosting the floating workspace cards: FileBrowser (top) and,
 * when the current conversation belongs to a project, ProjectTables (bottom).
 * The panel itself is transparent; each unit is a raised card with its own
 * rounded edge and shadow. Resize affordances (left edge for width, the gap
 * between cards for the split) are invisible until hovered.
 *
 * Home-screen-in-a-project case: when the root HomeComposer is on screen
 * (no conversation yet) while the Sidebar is drilled into a project, the URL
 * is "/" so the ``projectId`` prop is null -- but the user is looking at that
 * project and the first send will land in it, so the panel must show the
 * PROJECT'S workspace and tables, not the standalone empty state. The panel
 * therefore falls back to the context ``drilledProjectId`` when it has no
 * conversation, and -- because every file endpoint is keyed by conversation id
 * while all conversations of a project share one workspace directory
 * (data/projects/<pid>/workspace, see ChatStorage.get_workspace_path) -- it
 * borrows the id of any existing conversation in that project as the
 * FileBrowser's workspace handle. A project with no conversations at all has
 * nothing to borrow and renders the FileBrowser's project empty state.
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { FileBrowser } from './FileBrowser';
import { conversationSource, projectSource } from '../api/fileApi';
import { ProjectTables } from './ProjectTables';
import { useProjects } from '../contexts/ProjectsContext';
import { fetchProjectConversations } from '../api/client';
import './RightPanel.css';

const MIN_PANEL_HEIGHT = 80; // px minimum for each section
const DEFAULT_SPLIT_PERCENT = 60;

// Horizontal width of the right panel. The current/default width (280px) is the floor;
// the user can only widen the panel. Persisted browser-wide (not per-project/conversation).
const MIN_PANEL_WIDTH = 280; // px = current width = floor = default
const WIDTH_STORAGE_KEY = 'quest_right_panel_width';

// Max width is viewport-relative so the chat panel keeps at least ~600px. Computed as a
// helper (not a frozen constant) so it re-evaluates against the live window width.
function getMaxPanelWidth(): number {
  return Math.min(700, window.innerWidth - 600);
}

function getStorageKey(projectId: string): string {
  return `quest_project_tables_split_${projectId}`;
}

interface RightPanelProps {
  conversationId: string | null;
  /** Project id from the URL (null at "/" and for standalone chats). */
  projectId: string | null;
}

/**
 * Resolve a conversation id whose workspace IS the project workspace, for use
 * as the FileBrowser handle while no conversation is selected. Any
 * conversation of the project will do (they all map to the same directory);
 * archived ones are included so a project whose chats were all archived still
 * shows its files. Returns null while loading, when the project has no
 * conversations, or when the lookup fails.
 */
function useProjectWorkspaceProxy(projectId: string | null): string | null {
  const [proxy, setProxy] = useState<{ projectId: string; conversationId: string | null } | null>(null);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    fetchProjectConversations(projectId, true)
      .then((response) => {
        if (cancelled) return;
        const first = response.conversations[0];
        setProxy({ projectId, conversationId: first ? first.id : null });
      })
      .catch(() => {
        if (cancelled) return;
        setProxy({ projectId, conversationId: null });
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  // Only honor a result that belongs to the CURRENT project, so switching
  // projects never briefly shows the previous project's files.
  if (!projectId || !proxy || proxy.projectId !== projectId) return null;
  return proxy.conversationId;
}

export function RightPanel({ conversationId, projectId: urlProjectId }: RightPanelProps) {
  const { drilledProjectId } = useProjects();

  // With a live conversation the URL decides (a standalone chat viewed while
  // the Sidebar happens to be drilled must NOT show project cards). Without
  // one -- the home composer -- the drilled project is what the user sees.
  const projectId = conversationId ? urlProjectId : (urlProjectId ?? drilledProjectId);

  // Workspace handle for the FileBrowser: the real conversation when there is
  // one, otherwise a borrowed conversation from the drilled project (if any).
  const proxyConversationId = useProjectWorkspaceProxy(conversationId ? null : projectId);
  const workspaceConversationId = conversationId ?? proxyConversationId;

  const containerRef = useRef<HTMLDivElement>(null);
  const [splitPercent, setSplitPercent] = useState(DEFAULT_SPLIT_PERCENT);
  const isDraggingRef = useRef(false);

  // Horizontal width of the panel (px). Browser-wide, read on mount only.
  const [panelWidth, setPanelWidth] = useState(MIN_PANEL_WIDTH);
  const isDraggingWidthRef = useRef(false);
  // Mirrors the two drag refs as state so the handles stay visible mid-drag even
  // when the pointer leaves the (thin) handle hit area.
  const [resizing, setResizing] = useState<'width' | 'split' | null>(null);

  // Load saved panel width on mount (read-once, no cross-tab storage listener).
  // Browser-wide key -> applies the same width across all conversations/projects.
  useEffect(() => {
    const stored = localStorage.getItem(WIDTH_STORAGE_KEY);
    if (stored) {
      const parsed = parseInt(stored, 10);
      if (Number.isFinite(parsed) && parsed >= MIN_PANEL_WIDTH) {
        // Clamp down to the current viewport-relative max so a value stored on a
        // wider screen is reined in for this window.
        const clamped = Math.max(MIN_PANEL_WIDTH, Math.min(getMaxPanelWidth(), parsed));
        setPanelWidth(clamped);
      }
    }

    // Re-clamp the width down to the live viewport max when the window resizes.
    const handleResize = () => {
      setPanelWidth((current) =>
        Math.max(MIN_PANEL_WIDTH, Math.min(getMaxPanelWidth(), current))
      );
    };
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // Handle horizontal width drag (left-edge handle). Dragging the handle left widens
  // the panel; dragging right narrows it (never below MIN_PANEL_WIDTH).
  const handleWidthMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    isDraggingWidthRef.current = true;
    setResizing('width');

    const startX = e.clientX;
    let startWidth = MIN_PANEL_WIDTH;
    setPanelWidth((current) => {
      startWidth = current;
      return current;
    });

    // Suppress text selection for the duration of the drag.
    const prevUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = 'none';

    const handleMouseMove = (moveEvent: MouseEvent) => {
      if (!isDraggingWidthRef.current) return;
      // Delta-based: moving left (clientX decreases) increases the width.
      const newWidth = startWidth + (startX - moveEvent.clientX);
      const clamped = Math.max(MIN_PANEL_WIDTH, Math.min(getMaxPanelWidth(), newWidth));
      setPanelWidth(clamped);
    };

    const handleMouseUp = () => {
      isDraggingWidthRef.current = false;
      setResizing(null);
      document.body.style.userSelect = prevUserSelect;

      // Save the final (clamped) width on drag end.
      setPanelWidth((current) => {
        const clamped = Math.max(MIN_PANEL_WIDTH, Math.min(getMaxPanelWidth(), current));
        localStorage.setItem(WIDTH_STORAGE_KEY, String(clamped));
        return clamped;
      });

      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  }, []);

  // Load saved split position from localStorage when projectId changes
  useEffect(() => {
    if (!projectId) return;
    const stored = localStorage.getItem(getStorageKey(projectId));
    if (stored) {
      const parsed = parseFloat(stored);
      if (!isNaN(parsed) && parsed >= 10 && parsed <= 90) {
        setSplitPercent(parsed);
      }
    } else {
      setSplitPercent(DEFAULT_SPLIT_PERCENT);
    }
  }, [projectId]);

  // Handle divider drag
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    isDraggingRef.current = true;
    setResizing('split');

    const container = containerRef.current;
    if (!container) return;

    const handleMouseMove = (moveEvent: MouseEvent) => {
      if (!isDraggingRef.current || !container) return;

      const containerRect = container.getBoundingClientRect();
      const containerHeight = containerRect.height;
      const mouseY = moveEvent.clientY - containerRect.top;

      // Calculate percentage and enforce minimums
      let newPercent = (mouseY / containerHeight) * 100;
      const minPercent = (MIN_PANEL_HEIGHT / containerHeight) * 100;
      const maxPercent = 100 - minPercent;
      newPercent = Math.max(minPercent, Math.min(maxPercent, newPercent));

      setSplitPercent(newPercent);
    };

    const handleMouseUp = () => {
      isDraggingRef.current = false;
      setResizing(null);

      // Save to localStorage on drag end
      if (projectId) {
        const container = containerRef.current;
        if (container) {
          const containerRect = container.getBoundingClientRect();
          const containerHeight = containerRect.height;
          const minPercent = (MIN_PANEL_HEIGHT / containerHeight) * 100;
          const maxPercent = 100 - minPercent;
          // Re-read current state for saving
          setSplitPercent((current) => {
            const clamped = Math.max(minPercent, Math.min(maxPercent, current));
            localStorage.setItem(getStorageKey(projectId), String(clamped));
            return clamped;
          });
        }
      }

      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  }, [projectId]);

  const panelClass = `right-panel${resizing ? ` resizing-${resizing}` : ''}`;

  // No project: render just the FileBrowser card at full height
  if (!projectId) {
    return (
      <div className={panelClass} ref={containerRef} style={{ width: panelWidth }}>
        <div
          className="right-panel-resize-handle"
          onMouseDown={handleWidthMouseDown}
          title="Drag to resize"
        />
        <div className="right-panel-section right-panel-card" style={{ flex: 1 }}>
          <FileBrowser source={workspaceConversationId ? conversationSource(workspaceConversationId) : null} />
        </div>
      </div>
    );
  }

  // With project: two stacked cards with a draggable gap between them
  return (
    <div className={panelClass} ref={containerRef} style={{ width: panelWidth }}>
      <div
        className="right-panel-resize-handle"
        onMouseDown={handleWidthMouseDown}
        title="Drag to resize"
      />
      <div
        className="right-panel-section right-panel-card"
        style={{ height: `${splitPercent}%`, flex: 'none' }}
      >
        <FileBrowser
          source={workspaceConversationId ? conversationSource(workspaceConversationId) : projectSource(projectId)}
        />
      </div>
      <div
        className="right-panel-divider"
        onMouseDown={handleMouseDown}
        title="Drag to resize"
      />
      <div className="right-panel-section right-panel-card" style={{ flex: 1 }}>
        <ProjectTables projectId={projectId} />
      </div>
    </div>
  );
}
