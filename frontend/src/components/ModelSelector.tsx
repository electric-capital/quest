/**
 * ModelSelector -- the composer's model menu.
 *
 * Replaces the old flat native <select>: the top level shows the admin's
 * slotted picks for the conversation's visibility (Settings > Model
 * Selection keeps separate private and public-project top levels: up to
 * five models each, shown as the descriptor -- e.g. "Smart ($$$)" --
 * sublabeled with the concrete model name, or as the bare model name when
 * the descriptor is empty; each pick is fixed to the thinking level the
 * admin chose for it, or the model's default) and an "All models" row that
 * opens a flyout submenu with the full model list.
 *
 * Under "All models", a model with selectable thinking levels (catalog
 * `thinkingEfforts`) gets a third-level flyout listing them, opened by
 * hovering or clicking its chevron; picking a level selects the model at
 * that level, while clicking the model row itself selects it at its
 * default level. The trigger shows the level after the model name when
 * one is picked ("Claude Opus 5.5 · High").
 *
 * The host passes the already-filtered model list (credentialed models,
 * allowed for the conversation's visibility, narrowed by the conversation's
 * provider lock); slotted picks that are not in that list are hidden. When
 * the current selection itself is not in the list (deprecated model, model
 * the admin disallowed for this visibility, or missing credentials), it is
 * kept visible -- suffixed "(deprecated)" / "(not allowed here)" / "(no
 * credentials)" -- so the user can see and switch away from it, matching
 * the old <select> behavior.
 *
 * Desktop interaction mirrors the Flags popover (opens upward, outside-click
 * close) plus Escape-to-close and hover-or-click to open the submenu.
 * The thinking flyout opens right of the "All models" list, clear of its
 * scrollbar. Both flyouts stay open while the pointer travels to them
 * (useFlyoutGrace): a diagonal path across other rows, the gap, or the
 * list's scrollbar does not switch or close them.
 *
 * Phone widths (useIsMobile) get a full-screen sheet instead of the popover:
 * a header with a close button over ONE scrolling list -- the top-level
 * picks, then every model under an "All models" heading (no drill-in; a
 * model's thinking levels are a row of chips under its name). A popover
 * anchored above the composer cannot fit a long model list in the strip
 * left above the on-screen keyboard, so the sheet also dismisses the
 * keyboard while it is open and hands focus back to the composer on close.
 */

import { useEffect, useLayoutEffect, useRef, useState, useCallback, type Ref } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import {
  formatModelPick, formatThinkingEffort, getModelDisplayName, getModelInfo, getTopLevelModels,
  isDeprecatedModel, isModelAllowedFor,
} from '../constants/models';
import type { ModelInfo, ModelVisibility } from '../constants/models';
import { useFlyoutGrace } from '../hooks/useFlyoutGrace';
import { useIsMobile } from '../hooks/useIsMobile';

export interface ModelSelectorProps {
  /** Currently selected model id ('' allowed in the read-only case). */
  selectedModel: string;
  /** Thinking level picked for the selected model (null = its default). */
  selectedThinkingEffort?: string | null;
  /**
   * Selectable models: credentialed, and already narrowed by the
   * conversation's provider lock when one is set.
   */
  models: ModelInfo[];
  /** A pick: the model and the level chosen for it (null = default). */
  onSelect: (modelId: string, thinkingEffort: string | null) => void;
  disabled?: boolean;
  /**
   * Optional trigger-label override for the read-only (Slack) case, e.g.
   * "Server default" when the conversation has no explicit model.
   */
  labelOverride?: string;
  /**
   * Visibility of the conversation the menu is for: selects which admin
   * top level (private or public) to show and explains why a current
   * selection is not in `models` ("(not allowed here)").
   */
  visibility?: ModelVisibility;
  /**
   * Reports the menu opening and closing. The phone composer keeps its
   * controls row (and with it this component) mounted while the sheet is
   * open even though the sheet takes focus away from the textarea.
   */
  onOpenChange?: (open: boolean) => void;
}

/** Where the third-level thinking flyout sits: right of the "All models"
 *  list (outside its scrollbar), growing upward from its model row's bottom
 *  edge (the menu opens above the composer at the bottom of the screen).
 *  Fixed, so the scrolling submenu does not clip it. */
interface EffortFlyoutAnchor {
  modelId: string;
  left: number;
  bottom: number;
}

export function ModelSelector({
  selectedModel,
  selectedThinkingEffort = null,
  models,
  onSelect,
  disabled = false,
  labelOverride,
  visibility = 'private',
  onOpenChange,
}: ModelSelectorProps) {
  const isMobile = useIsMobile();
  const [isOpen, setIsOpen] = useState(false);
  const [isSubmenuOpen, setIsSubmenuOpen] = useState(false);
  const [effortFlyout, setEffortFlyout] = useState<EffortFlyoutAnchor | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const submenuRef = useRef<HTMLDivElement>(null);
  const effortFlyoutRef = useRef<HTMLDivElement>(null);
  // The current selection's row in the "All models" list.
  const submenuSelectedRef = useRef<HTMLButtonElement>(null);
  // Pointer grace on the way from "All models" to its list, and from a model
  // row to its thinking flyout.
  const submenuGrace = useFlyoutGrace();
  const effortGrace = useFlyoutGrace();
  const sheetRef = useRef<HTMLDivElement>(null);
  // Element that had focus when the phone sheet opened (the composer
  // textarea, i.e. the keyboard was up); focus returns there on close.
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  // Row the phone sheet scrolls to when it opens (see selectionIsTopLevel).
  const sheetScrollTargetRef = useRef<HTMLButtonElement>(null);
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;

  const open = useCallback(() => {
    const active = document.activeElement;
    restoreFocusRef.current = isMobile && active instanceof HTMLElement && active !== document.body
      ? active
      : null;
    // Notified in the same batch as the open (not from an effect): the host
    // must already know by the time the sheet effect below blurs the
    // textarea, or the phone composer would collapse and unmount this menu.
    onOpenChangeRef.current?.(true);
    setIsOpen(true);
  }, [isMobile]);

  const close = useCallback(() => {
    // Synchronous, so that on a phone it still runs inside the tap that
    // closed the sheet -- iOS only raises the keyboard for a focus() made
    // during a user gesture.
    restoreFocusRef.current?.focus();
    restoreFocusRef.current = null;
    submenuGrace.end();
    effortGrace.end();
    setIsOpen(false);
    setIsSubmenuOpen(false);
    setEffortFlyout(null);
    onOpenChangeRef.current?.(false);
  }, [submenuGrace, effortGrace]);

  // An unmount while open (conversation switch, composer going read-only)
  // must not leave the host believing the menu is still up.
  useEffect(() => () => onOpenChangeRef.current?.(false), []);

  // Outside-click close (Flags popover pattern). The phone sheet is portaled
  // out of the container, so a tap inside it is not an outside click.
  useEffect(() => {
    if (!isOpen) return;
    const handleClickOutside = (event: MouseEvent) => {
      const target = event.target as Node;
      if (containerRef.current?.contains(target) || sheetRef.current?.contains(target)) return;
      close();
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isOpen, close]);

  // Escape close.
  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, close]);

  // Phone sheet: move focus from the composer textarea into the sheet, which
  // drops the on-screen keyboard so the list gets the whole screen, and
  // bring the current selection into view. Once per open, from an effect: a
  // ref callback would re-run on every host re-render and yank the list
  // back while the user scrolls it.
  useEffect(() => {
    if (!isOpen || !isMobile) return;
    const active = document.activeElement;
    if (active instanceof HTMLElement && active !== document.body) active.blur();
    sheetRef.current?.focus({ preventScroll: true });
    sheetScrollTargetRef.current?.scrollIntoView({ block: 'center' });
  }, [isOpen, isMobile]);

  const handlePick = useCallback((modelId: string, thinkingEffort: string | null) => {
    onSelect(modelId, thinkingEffort);
    close();
  }, [onSelect, close]);

  // Bring the current selection into view in the "All models" list, which
  // scrolls once it outgrows its max-height. Once per opening: re-running on
  // every render would yank a list the user scrolled back to the selection
  // (and the scroll would close the thinking flyout being hovered).
  useLayoutEffect(() => {
    if (isSubmenuOpen) submenuSelectedRef.current?.scrollIntoView({ block: 'nearest' });
  }, [isSubmenuOpen]);

  const closeSubmenu = useCallback(() => {
    submenuGrace.end();
    effortGrace.end();
    setIsSubmenuOpen(false);
    setEffortFlyout(null);
  }, [submenuGrace, effortGrace]);

  // Open the thinking flyout for a model row (desktop "All models" list):
  // right of the list's outer edge, so the list's scrollbar is never painted
  // over it, level with the row.
  const openEffortFlyout = useCallback((modelId: string, row: HTMLElement) => {
    const list = row.closest('.model-submenu') ?? row;
    const left = list.getBoundingClientRect().right;
    const bottom = window.innerHeight - row.getBoundingClientRect().bottom;
    setEffortFlyout((prev) => (prev?.modelId === modelId ? prev : { modelId, left, bottom }));
  }, []);

  // The hover a row would apply: its own flyout when it has levels, none
  // otherwise. `under` is any element (e.g. the one a grace ended over).
  const hoverEffortRow = useCallback((under: Element | null) => {
    const row = under?.closest<HTMLElement>('.model-effort-anchor');
    const modelId = row?.dataset.modelId;
    if (row && modelId && models.some((m) => m.id === modelId && m.thinkingEfforts.length > 0)) {
      openEffortFlyout(modelId, row);
    } else {
      setEffortFlyout(null);
    }
  }, [models, openEffortFlyout]);

  // The admin's top-level picks for this visibility, limited to what is
  // actually selectable here.
  const topLevel = getTopLevelModels(visibility).filter(
    (t) => models.some((m) => m.id === t.id),
  );

  // Keep a current selection that isn't offered (deprecated model, model
  // disallowed for this visibility, or credentials missing) visible in the
  // full list so the user can see what is selected (and switch away from it).
  const selectionMissing = selectedModel !== ''
    && !models.some((m) => m.id === selectedModel);
  const selectedInfo = getModelInfo(selectedModel);
  const missingSuffix = isDeprecatedModel(selectedModel)
    ? ' (deprecated)'
    : selectedInfo && !isModelAllowedFor(selectedInfo, visibility)
      ? ' (not allowed here)'
      : ' (no credentials)';
  const triggerLabel = labelOverride
    ?? (formatModelPick(selectedModel, selectedThinkingEffort) + (selectionMissing ? missingSuffix : ''));

  // The current selection when it isn't offered, heading the full list.
  const missingSelectionItem = selectionMissing && (
    <ModelMenuItem
      label={getModelDisplayName(selectedModel) + missingSuffix}
      selected
      onClick={() => handlePick(selectedModel, selectedThinkingEffort)}
    />
  );

  // A slotted pick is "the selection" when both its model and its fixed
  // level match what the conversation runs at.
  const isPickSelected = (pick: ModelInfo) =>
    pick.id === selectedModel && (pick.slotEffort ?? null) === (selectedThinkingEffort ?? null);

  // Sublabel of a slotted pick: the model name under a descriptor, with the
  // fixed level after it when the admin set one.
  const pickSub = (pick: ModelInfo): string | undefined => {
    const level = pick.slotEffort ? formatThinkingEffort(pick.slotEffort) : null;
    if (pick.descriptor) return level ? `${pick.name} · ${level}` : pick.name;
    return level ? `${level} thinking` : undefined;
  };

  // In the phone sheet the top-level picks sit at the top of the list, so a
  // slotted selection is already on screen; only a selection that appears
  // solely under "All models" needs scrolling to.
  const selectionIsTopLevel = topLevel.some(isPickSelected);

  const effortLabel = (model: ModelInfo, effort: string) =>
    formatThinkingEffort(effort) + (effort === model.defaultThinkingEffort ? ' (default)' : '');

  const sheet = (
    <div
      className="model-sheet"
      role="dialog"
      aria-modal="true"
      aria-label="Choose a model"
      tabIndex={-1}
      ref={sheetRef}
      // React bubbles a portal's events to its React parents. Keep the
      // sheet's focus changes away from the composer's focus-within
      // tracking, which would otherwise read them as focus in the composer.
      onFocus={(e) => e.stopPropagation()}
      onBlur={(e) => e.stopPropagation()}
    >
      <div className="model-sheet-header">
        <h2>Model</h2>
        <button type="button" className="model-sheet-close" onClick={close} aria-label="Close">
          <X size={22} />
        </button>
      </div>
      <div className="model-sheet-list">
        {topLevel.map((pick) => (
          <ModelMenuItem
            key={pick.id}
            label={pick.descriptor || pick.name}
            sub={pickSub(pick)}
            selected={isPickSelected(pick)}
            onClick={() => handlePick(pick.id, pick.slotEffort)}
          />
        ))}
        {topLevel.length > 0 && <div className="model-sheet-heading">All models</div>}
        {missingSelectionItem}
        {models.map((model) => (
          <div key={model.id} className="model-sheet-entry">
            <ModelMenuItem
              label={model.name}
              selected={model.id === selectedModel}
              itemRef={model.id === selectedModel && !selectionIsTopLevel
                ? sheetScrollTargetRef
                : undefined}
              onClick={() => handlePick(model.id, null)}
            />
            {model.thinkingEfforts.length > 0 && (
              <div className="model-effort-chips" role="group" aria-label={`Thinking level for ${model.name}`}>
                {model.thinkingEfforts.map((effort) => {
                  const chipSelected = model.id === selectedModel && effort === selectedThinkingEffort;
                  return (
                    <button
                      key={effort}
                      type="button"
                      className={'model-effort-chip' + (chipSelected ? ' model-effort-chip-selected' : '')}
                      aria-pressed={chipSelected}
                      onClick={() => handlePick(model.id, effort)}
                    >
                      {effortLabel(model, effort)}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );

  return (
    <div className="model-menu-container" ref={containerRef}>
      <button
        type="button"
        id="model-select"
        className="model-menu-trigger"
        onClick={() => (isOpen ? close() : open())}
        disabled={disabled}
        aria-haspopup={isMobile ? 'dialog' : 'menu'}
        aria-expanded={isOpen}
        title="Choose the model for this conversation"
      >
        <span className="model-menu-trigger-label">{triggerLabel}</span>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="18 15 12 9 6 15"></polyline>
        </svg>
      </button>
      {/* The sheet mounts inside the phone shell, which MobileShell keeps
          sized to the visual viewport, so it covers exactly the visible area
          even while the keyboard is still sliding away. */}
      {isOpen && isMobile && createPortal(
        sheet,
        document.querySelector('.mobile-shell') ?? document.body,
      )}
      {isOpen && !isMobile && (
        <div className="model-menu" role="menu">
          {topLevel.map((pick) => (
            <ModelMenuItem
              key={pick.id}
              role="menuitem"
              label={pick.descriptor || pick.name}
              sub={pickSub(pick)}
              selected={isPickSelected(pick)}
              onClick={() => handlePick(pick.id, pick.slotEffort)}
              onMouseEnter={(e) => {
                if (!submenuGrace.isHeadingToFlyout(e)) closeSubmenu();
              }}
            />
          ))}
          {topLevel.length > 0 && <div className="model-menu-divider" />}
          <div
            className="model-submenu-anchor"
            onMouseEnter={() => {
              submenuGrace.end();
              setIsSubmenuOpen(true);
            }}
            onMouseLeave={(e) => {
              if (!isSubmenuOpen) return;
              // A pointer that stopped on, or moved off the way onto, a
              // top-level pick closes the list as hovering the pick would.
              submenuGrace.begin(e, submenuRef.current, (under) => {
                if (under?.closest('.model-menu-item') && !under.closest('.model-submenu-anchor')) closeSubmenu();
              });
            }}
          >
            <button
              type="button"
              role="menuitem"
              aria-haspopup="menu"
              aria-expanded={isSubmenuOpen}
              className={'model-menu-item' + (isSubmenuOpen ? ' model-menu-item-open' : '')}
              // Open (not toggle) on click: hover already opens the submenu,
              // so a toggle would close it again on the click that follows
              // the hover.
              onClick={() => setIsSubmenuOpen(true)}
            >
              <span className="model-menu-item-text">
                <span className="model-menu-item-label">All models</span>
              </span>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="9 18 15 12 9 6"></polyline>
              </svg>
            </button>
            {isSubmenuOpen && (
              <div
                className="model-submenu"
                role="menu"
                ref={submenuRef}
                // The thinking flyout is pinned to its row's screen position;
                // scrolling the list would leave it behind, so drop it.
                onScroll={() => {
                  effortGrace.end();
                  setEffortFlyout(null);
                }}
              >
                {missingSelectionItem}
                {models.map((model) => {
                  const hasEfforts = model.thinkingEfforts.length > 0;
                  const flyoutOpen = effortFlyout?.modelId === model.id;
                  return (
                    <div
                      key={model.id}
                      className="model-effort-anchor"
                      data-model-id={model.id}
                      onMouseEnter={(e) => {
                        // Crossing this row on the way to another row's
                        // flyout leaves that flyout up.
                        if (!flyoutOpen && effortGrace.isHeadingToFlyout(e)) return;
                        effortGrace.end();
                        hoverEffortRow(e.currentTarget);
                      }}
                      onMouseLeave={(e) => {
                        if (!flyoutOpen) return;
                        // Toward the flyout: keep it while the pointer gets
                        // there; once the grace gives up, apply the hover of
                        // whatever row the pointer is on by then.
                        if (effortGrace.begin(e, effortFlyoutRef.current, hoverEffortRow)) return;
                        setEffortFlyout(null);
                      }}
                    >
                      <ModelMenuItem
                        role="menuitem"
                        label={model.name}
                        sub={hasEfforts && model.id === selectedModel && selectedThinkingEffort
                          ? `${formatThinkingEffort(selectedThinkingEffort)} thinking`
                          : undefined}
                        selected={model.id === selectedModel}
                        open={flyoutOpen}
                        submenu={hasEfforts}
                        itemRef={model.id === selectedModel ? submenuSelectedRef : undefined}
                        // The row itself picks the model at its default level;
                        // the chevron only opens the level flyout.
                        onClick={() => handlePick(model.id, null)}
                        onChevronClick={hasEfforts
                          ? (e) => openEffortFlyout(model.id, e.currentTarget.closest('.model-effort-anchor') as HTMLElement)
                          : undefined}
                      />
                      {flyoutOpen && effortFlyout && (
                        <div
                          className="model-submenu model-effort-submenu"
                          role="menu"
                          ref={effortFlyoutRef}
                          aria-label={`Thinking level for ${model.name}`}
                          style={{ left: effortFlyout.left, bottom: effortFlyout.bottom }}
                        >
                          {model.thinkingEfforts.map((effort) => (
                            <ModelMenuItem
                              key={effort}
                              role="menuitem"
                              label={effortLabel(model, effort)}
                              selected={model.id === selectedModel && effort === selectedThinkingEffort}
                              onClick={() => handlePick(model.id, effort)}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

interface ModelMenuItemProps {
  label: string;
  /** Concrete model name under a descriptor label. */
  sub?: string;
  selected: boolean;
  onClick: () => void;
  onMouseEnter?: (event: React.MouseEvent<HTMLButtonElement>) => void;
  role?: string;
  itemRef?: Ref<HTMLButtonElement>;
  /** Row that opens a further flyout: shows a chevron (clickable on its own). */
  submenu?: boolean;
  /** Whether that flyout is open (hover styling, aria-expanded). */
  open?: boolean;
  onChevronClick?: (event: React.MouseEvent<HTMLSpanElement>) => void;
}

/** One pickable model row, shared by the desktop menus and the phone sheet. */
function ModelMenuItem({
  label, sub, selected, onClick, onMouseEnter, role, itemRef, submenu, open, onChevronClick,
}: ModelMenuItemProps) {
  return (
    <button
      type="button"
      role={role}
      aria-current={selected ? 'true' : undefined}
      aria-haspopup={submenu ? 'menu' : undefined}
      aria-expanded={submenu ? Boolean(open) : undefined}
      className={'model-menu-item' + (selected ? ' model-menu-item-selected' : '') + (open ? ' model-menu-item-open' : '')}
      ref={itemRef}
      onClick={onClick}
      onMouseEnter={onMouseEnter}
    >
      <span className="model-menu-item-text">
        <span className="model-menu-item-label">{label}</span>
        {sub && <span className="model-menu-item-sub">{sub}</span>}
      </span>
      {selected && <CheckIcon />}
      {submenu && (
        <span
          className="model-menu-item-chevron"
          onClick={onChevronClick && ((e) => { e.stopPropagation(); onChevronClick(e); })}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="9 18 15 12 9 6"></polyline>
          </svg>
        </span>
      )}
    </button>
  );
}

function CheckIcon() {
  return (
    <svg className="model-menu-check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12"></polyline>
    </svg>
  );
}
