/**
 * Right panel wrapper hosting the floating workspace cards. The panel itself
 * is transparent; each unit is a raised card with its own rounded edge and
 * shadow. Resize affordances (left edge for width, the gaps between cards for
 * the vertical split) are invisible until hovered.
 *
 * Card set, by what is on screen:
 * - project conversation: Chat Files (the conversation's own workspace),
 *   Project Files (the project's shared workspace) and Tables, with Copy /
 *   Move row actions between the two file cards (useWorkspaceCopy);
 * - standalone conversation: Chat Files alone, full height;
 * - no conversation inside a project (the home composer while the Sidebar
 *   is drilled into one -- the URL is "/", so the project comes from the
 *   context ``drilledProjectId``): Project Files and Tables, served by the
 *   project routes; there is no chat yet, so no Chat Files card;
 * - nothing: the FileBrowser's empty state.
 *
 * Vertical split: every card is a flex item weighted by its share of the
 * column (percentages summing to 100), so the fixed-height gaps never push
 * the last card below its minimum. The shares are persisted per project in
 * localStorage, one key per layout: the two-card layout keeps the historical
 * ``quest_project_tables_split_<pid>`` key (the top card's percentage), the
 * three-card layout uses ``quest_project_panel_split3_<pid>`` (JSON
 * ``[chat, project]`` percentages, Tables takes the rest) and, until the
 * user drags it, derives its shares from the two-card key so an existing
 * Tables height carries over.
 */

import { Fragment, useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { FileBrowser } from './FileBrowser';
import { conversationSource, projectSource } from '../api/fileApi';
import { ProjectTables } from './ProjectTables';
import { DocConfirmDialog } from './docs/DocConfirmDialog';
import { useProjects } from '../contexts/ProjectsContext';
import { overwritePromptText, useWorkspaceCopy } from '../hooks/useWorkspaceCopy';
import './RightPanel.css';

const MIN_PANEL_HEIGHT = 80; // px minimum for each section
const DEFAULT_SPLIT_PERCENT = 60;
/** Default three-card shares: Chat Files, Project Files (Tables = the rest). */
const DEFAULT_SPLIT3: readonly [number, number] = [40, 35];

// Horizontal width of the right panel. The current/default width (280px) is the floor;
// the user can only widen the panel. Persisted browser-wide (not per-project/conversation).
const MIN_PANEL_WIDTH = 280; // px = current width = floor = default
const WIDTH_STORAGE_KEY = 'quest_right_panel_width';

// Max width is viewport-relative so the chat panel keeps at least ~600px. Computed as a
// helper (not a frozen constant) so it re-evaluates against the live window width.
function getMaxPanelWidth(): number {
  return Math.min(700, window.innerWidth - 600);
}

/** Two-card split key (top card's percentage); the pre-three-card key, kept as is. */
export function getSplitStorageKey(projectId: string): string {
  return `quest_project_tables_split_${projectId}`;
}

/** Three-card split key: JSON `[chatPercent, projectPercent]`. */
export function getSplit3StorageKey(projectId: string): string {
  return `quest_project_panel_split3_${projectId}`;
}

export type Layout = 'single' | 'two' | 'three';

function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Storage unavailable (private mode, quota): the split just isn't kept.
  }
}

function readTwoCardSplit(projectId: string): number | null {
  const stored = readStorage(getSplitStorageKey(projectId));
  if (!stored) return null;
  const parsed = parseFloat(stored);
  return Number.isFinite(parsed) && parsed >= 10 && parsed <= 90 ? parsed : null;
}

/**
 * Smallest share a loaded card may have (percent). The real floor is the
 * 80px card `min-height`; this only keeps a stored value from wedging a card
 * at (near) zero so its divider stays reachable.
 */
const MIN_SHARE_PERCENT = 5;

/**
 * Scale shares to sum to 100 and lift every share to at least `minShare`,
 * taking the difference from the cards above the floor in proportion to
 * their excess. Null for garbage (non-finite / non-positive values) or when
 * the floor cannot fit.
 */
export function normalizeShares(raw: number[], minShare = MIN_SHARE_PERCENT): number[] | null {
  if (raw.length === 0 || raw.some((v) => !Number.isFinite(v) || v <= 0)) return null;
  if (raw.length * minShare > 100) return null;
  const total = raw.reduce((x, y) => x + y, 0);
  const scaled = raw.map((v) => (v / total) * 100);
  const deficit = scaled.reduce((acc, v) => acc + Math.max(0, minShare - v), 0);
  if (deficit === 0) return scaled;
  const excess = scaled.reduce((acc, v) => acc + Math.max(0, v - minShare), 0);
  if (excess < deficit) return null;
  return scaled.map((v) => (v <= minShare ? minShare : v - ((v - minShare) / excess) * deficit));
}

function defaultSizes(layout: Layout): number[] {
  if (layout === 'two') return [DEFAULT_SPLIT_PERCENT, 100 - DEFAULT_SPLIT_PERCENT];
  if (layout === 'three') return [DEFAULT_SPLIT3[0], DEFAULT_SPLIT3[1], 100 - DEFAULT_SPLIT3[0] - DEFAULT_SPLIT3[1]];
  return [100];
}

function readSplit3(projectId: string): number[] | null {
  const stored = readStorage(getSplit3StorageKey(projectId));
  if (!stored) return null;
  try {
    const parsed: unknown = JSON.parse(stored);
    if (!Array.isArray(parsed) || parsed.length !== 2) return null;
    const [chat, project] = parsed as unknown[];
    if (typeof chat !== 'number' || typeof project !== 'number') return null;
    return normalizeShares([chat, project, 100 - chat - project]);
  } catch {
    return null; // Malformed value: the caller falls back.
  }
}

/** Card shares (percent, summing to 100) for a layout, from storage or defaults. */
export function loadSizes(layout: Layout, projectId: string | null): number[] {
  if (layout === 'single' || !projectId) return [100];
  const two = readTwoCardSplit(projectId);
  if (layout === 'two') {
    return two !== null ? [two, 100 - two] : defaultSizes('two');
  }
  const three = readSplit3(projectId);
  if (three) return three;
  if (two !== null) {
    // The old two-card split put Tables at `100 - two`; keep that and share
    // the file area evenly between the two file cards.
    return normalizeShares([two / 2, two / 2, 100 - two]) ?? defaultSizes('three');
  }
  return defaultSizes('three');
}

function saveSizes(layout: Layout, projectId: string, sizes: number[]): void {
  if (layout === 'two') {
    writeStorage(getSplitStorageKey(projectId), String(sizes[0]));
  } else if (layout === 'three') {
    writeStorage(getSplit3StorageKey(projectId), JSON.stringify([sizes[0], sizes[1]]));
  }
}

/**
 * Move the boundary below card `index` to `pointerPercent` (a position in
 * the cards' free space, as a percentage of it), keeping both neighbours at
 * least `minPercent`. Other cards are untouched. When the two neighbours
 * cannot both fit their minimum, the pair is split evenly.
 */
export function moveSplitBoundary(
  sizes: number[],
  index: number,
  pointerPercent: number,
  minPercent: number,
): number[] {
  const lo = sizes.slice(0, index).reduce((a, b) => a + b, 0);
  const pair = sizes[index] + sizes[index + 1];
  const next = [...sizes];
  if (pair < 2 * minPercent) {
    next[index] = pair / 2;
    next[index + 1] = pair / 2;
    return next;
  }
  const upper = Math.max(minPercent, Math.min(pair - minPercent, pointerPercent - lo));
  next[index] = upper;
  next[index + 1] = pair - upper;
  return next;
}

/**
 * Height (px) the cards share in the column: the panel's height minus its
 * vertical padding and the divider gaps. The card shares are fractions of it.
 */
function measureFreeHeight(container: HTMLElement): number {
  const rect = container.getBoundingClientRect();
  const style = window.getComputedStyle(container);
  const padding = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
  let dividers = 0;
  container.querySelectorAll(':scope > .right-panel-divider').forEach((el) => {
    dividers += el.getBoundingClientRect().height;
  });
  return rect.height - padding - dividers;
}

interface RightPanelProps {
  conversationId: string | null;
  /** Project id from the URL (null at "/" and for standalone chats). */
  projectId: string | null;
}

export function RightPanel({ conversationId, projectId: urlProjectId }: RightPanelProps) {
  const { drilledProjectId } = useProjects();

  // With a live conversation the URL decides (a standalone chat viewed while
  // the Sidebar happens to be drilled must NOT show project cards). Without
  // one -- the home composer -- the drilled project is what the user sees.
  const projectId = conversationId ? urlProjectId : (urlProjectId ?? drilledProjectId);

  const layout: Layout = !projectId ? 'single' : conversationId ? 'three' : 'two';

  const chatSource = useMemo(
    () => (conversationId ? conversationSource(conversationId) : null),
    [conversationId],
  );
  const projectFilesSource = useMemo(
    () => (projectId ? projectSource(projectId) : null),
    [projectId],
  );

  // Copy / Move between the two file cards: actions only in a project conversation.
  const copy = useWorkspaceCopy(conversationId, conversationId ? projectId : null);

  const containerRef = useRef<HTMLDivElement>(null);
  const isDraggingRef = useRef(false);

  // Card shares while dragging (or after a drag), for the layout+project they
  // were made in; otherwise the stored/default shares for what is on screen.
  const layoutKey = `${layout}:${projectId ?? ''}`;
  const [dragSizes, setDragSizes] = useState<{ key: string; sizes: number[] } | null>(null);
  const loadedSizes = useMemo(() => loadSizes(layout, projectId), [layout, projectId]);
  const sizes = dragSizes && dragSizes.key === layoutKey ? dragSizes.sizes : loadedSizes;
  // Live shares for the drag handler (so it need not be rebuilt per render).
  const sizesRef = useRef(sizes);
  sizesRef.current = sizes;

  // Horizontal width of the panel (px). Browser-wide, read on mount only.
  const [panelWidth, setPanelWidth] = useState(MIN_PANEL_WIDTH);
  const isDraggingWidthRef = useRef(false);
  // Mirrors the drag refs as state so the handles stay visible mid-drag even
  // when the pointer leaves the (thin) handle hit area.
  const [resizing, setResizing] = useState<'width' | 'split' | null>(null);
  // The divider being dragged, so only its pill lights up.
  const [activeDivider, setActiveDivider] = useState<number | null>(null);

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

  // Divider drag: divider `index` sits between card `index` and `index + 1`.
  // Pointer events with capture, so mouse, pen and touch (the MobileShell
  // drawer) all drag. Delta-based against the cards' free space (panel
  // height minus padding and gaps): no jump on pointer down, and the 80px
  // minimum is measured in the same space the shares divide.
  const handleDividerPointerDown = useCallback((index: number, e: React.PointerEvent<HTMLDivElement>) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    e.preventDefault();
    const container = containerRef.current;
    if (!container) return;
    const handle = e.currentTarget;
    const pointerId = e.pointerId;
    try {
      handle.setPointerCapture?.(pointerId);
    } catch {
      // Capture unsupported: the drag still works while over the handle.
    }
    isDraggingRef.current = true;
    setResizing('split');
    setActiveDivider(index);

    const key = layoutKey;
    const startSizes = sizesRef.current;
    const startY = e.clientY;
    const startBoundary = startSizes.slice(0, index + 1).reduce((a, b) => a + b, 0);
    let current = startSizes;
    let moved = false;

    const handleMove = (moveEvent: PointerEvent) => {
      if (!isDraggingRef.current || moveEvent.pointerId !== pointerId) return;
      const free = measureFreeHeight(container);
      if (free <= 0) return;
      const pointerPercent = startBoundary + ((moveEvent.clientY - startY) / free) * 100;
      const minPercent = (MIN_PANEL_HEIGHT / free) * 100;
      const next = moveSplitBoundary(startSizes, index, pointerPercent, minPercent);
      if (next.some((v, i) => v !== current[i])) {
        current = next;
        moved = true;
        setDragSizes({ key, sizes: current });
      }
    };

    const handleEnd = (endEvent: PointerEvent) => {
      if (endEvent.pointerId !== pointerId) return;
      isDraggingRef.current = false;
      setResizing(null);
      setActiveDivider(null);
      // Save on drag end, and only when the split actually changed.
      if (moved && projectId) saveSizes(layout, projectId, current);
      handle.removeEventListener('pointermove', handleMove);
      handle.removeEventListener('pointerup', handleEnd);
      handle.removeEventListener('pointercancel', handleEnd);
    };

    handle.addEventListener('pointermove', handleMove);
    handle.addEventListener('pointerup', handleEnd);
    handle.addEventListener('pointercancel', handleEnd);
  }, [layout, layoutKey, projectId]);

  const panelClass = `right-panel${resizing ? ` resizing-${resizing}` : ''}`;

  const cards: { key: string; node: React.ReactNode }[] = [];
  if (chatSource || !projectFilesSource) {
    cards.push({
      key: 'chat',
      node: (
        <FileBrowser
          source={chatSource}
          rowActions={copy.chatRowActions}
          notice={copy.notices.conversation}
          onDismissNotice={() => copy.dismissNotice('conversation')}
        />
      ),
    });
  }
  if (projectFilesSource && projectId) {
    cards.push({
      key: 'project',
      node: (
        <FileBrowser
          source={projectFilesSource}
          rowActions={copy.projectRowActions}
          notice={copy.notices.project}
          onDismissNotice={() => copy.dismissNotice('project')}
        />
      ),
    });
    cards.push({ key: 'tables', node: <ProjectTables projectId={projectId} /> });
  }

  const prompt = copy.overwritePrompt;
  const promptText = prompt ? overwritePromptText(prompt) : null;

  return (
    <div className={panelClass} ref={containerRef} style={{ width: panelWidth }}>
      <div
        className="right-panel-resize-handle"
        onMouseDown={handleWidthMouseDown}
        title="Drag to resize"
      />
      {cards.map((card, i) => (
        <Fragment key={card.key}>
          {i > 0 && (
            <div
              className={`right-panel-divider${activeDivider === i - 1 ? ' active' : ''}`}
              data-testid={`right-panel-divider-${i - 1}`}
              onPointerDown={(e) => handleDividerPointerDown(i - 1, e)}
              title="Drag to resize"
            />
          )}
          <div
            className="right-panel-section right-panel-card"
            data-testid={`right-panel-card-${card.key}`}
            style={cards.length > 1
              ? { flex: `${sizes[i] ?? 1} 1 0px`, minHeight: MIN_PANEL_HEIGHT }
              : { flex: 1 }}
          >
            {card.node}
          </div>
        </Fragment>
      ))}
      <DocConfirmDialog
        isOpen={prompt !== null}
        title={promptText?.title ?? ''}
        confirmLabel={promptText?.confirmLabel ?? 'Replace'}
        busyLabel="Replacing..."
        tone="danger"
        busy={copy.overwriteBusy}
        error={copy.overwriteError}
        onConfirm={copy.confirmOverwrite}
        onClose={copy.cancelOverwrite}
      >
        {promptText && <p>{promptText.body}</p>}
      </DocConfirmDialog>
    </div>
  );
}
