/**
 * ModelSelector -- the composer's two-level model menu.
 *
 * Replaces the old flat native <select>: the top level shows the admin's
 * slotted picks for the conversation's visibility (Settings > Model
 * Selection keeps separate private and public-project top levels: up to
 * five models each, shown as the descriptor -- e.g. "Smart ($$$)" --
 * sublabeled with the concrete model name, or as the bare model name when
 * the descriptor is empty) and an "All models" row that opens a flyout
 * submenu with the full model list.
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
 *
 * Phone widths (useIsMobile) get a full-screen sheet instead of the popover:
 * a header with a close button over ONE scrolling list -- the top-level
 * picks, then every model under an "All models" heading (no drill-in). A
 * popover anchored above the composer cannot fit a long model list in the
 * strip left above the on-screen keyboard, so the sheet also dismisses the
 * keyboard while it is open and hands focus back to the composer on close.
 */

import { useEffect, useRef, useState, useCallback, type Ref } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import {
  getModelDisplayName, getModelInfo, getTopLevelModels, isDeprecatedModel, isModelAllowedFor,
} from '../constants/models';
import type { ModelInfo, ModelVisibility } from '../constants/models';
import { useIsMobile } from '../hooks/useIsMobile';

export interface ModelSelectorProps {
  /** Currently selected model id ('' allowed in the read-only case). */
  selectedModel: string;
  /**
   * Selectable models: credentialed, and already narrowed by the
   * conversation's provider lock when one is set.
   */
  models: ModelInfo[];
  onSelect: (modelId: string) => void;
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

export function ModelSelector({
  selectedModel,
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
  const containerRef = useRef<HTMLDivElement>(null);
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
    setIsOpen(false);
    setIsSubmenuOpen(false);
    onOpenChangeRef.current?.(false);
  }, []);

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

  const handlePick = useCallback((modelId: string) => {
    onSelect(modelId);
    close();
  }, [onSelect, close]);

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
    ?? (getModelDisplayName(selectedModel) + (selectionMissing ? missingSuffix : ''));

  // The current selection when it isn't offered, heading the full list.
  const missingSelectionItem = selectionMissing && (
    <ModelMenuItem
      label={getModelDisplayName(selectedModel) + missingSuffix}
      selected
      onClick={() => handlePick(selectedModel)}
    />
  );

  // In the phone sheet the top-level picks sit at the top of the list, so a
  // slotted selection is already on screen; only a selection that appears
  // solely under "All models" needs scrolling to.
  const selectionIsTopLevel = topLevel.some((t) => t.id === selectedModel);

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
            sub={pick.descriptor ? pick.name : undefined}
            selected={pick.id === selectedModel}
            onClick={() => handlePick(pick.id)}
          />
        ))}
        {topLevel.length > 0 && <div className="model-sheet-heading">All models</div>}
        {missingSelectionItem}
        {models.map((model) => (
          <ModelMenuItem
            key={model.id}
            label={model.name}
            selected={model.id === selectedModel}
            itemRef={model.id === selectedModel && !selectionIsTopLevel
              ? sheetScrollTargetRef
              : undefined}
            onClick={() => handlePick(model.id)}
          />
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
              sub={pick.descriptor ? pick.name : undefined}
              selected={pick.id === selectedModel}
              onClick={() => handlePick(pick.id)}
              onMouseEnter={() => setIsSubmenuOpen(false)}
            />
          ))}
          {topLevel.length > 0 && <div className="model-menu-divider" />}
          <div
            className="model-submenu-anchor"
            onMouseEnter={() => setIsSubmenuOpen(true)}
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
              <div className="model-submenu" role="menu">
                {missingSelectionItem}
                {models.map((model) => (
                  <ModelMenuItem
                    key={model.id}
                    role="menuitem"
                    label={model.name}
                    selected={model.id === selectedModel}
                    // The submenu scrolls when the model list outgrows its
                    // max-height; bring the current selection into view on
                    // open so it isn't hidden below the fold.
                    itemRef={model.id === selectedModel
                      ? (el) => el?.scrollIntoView({ block: 'nearest' })
                      : undefined}
                    onClick={() => handlePick(model.id)}
                  />
                ))}
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
  onMouseEnter?: () => void;
  role?: string;
  itemRef?: Ref<HTMLButtonElement>;
}

/** One pickable model row, shared by the desktop menus and the phone sheet. */
function ModelMenuItem({
  label, sub, selected, onClick, onMouseEnter, role, itemRef,
}: ModelMenuItemProps) {
  return (
    <button
      type="button"
      role={role}
      aria-current={selected ? 'true' : undefined}
      className={'model-menu-item' + (selected ? ' model-menu-item-selected' : '')}
      ref={itemRef}
      onClick={onClick}
      onMouseEnter={onMouseEnter}
    >
      <span className="model-menu-item-text">
        <span className="model-menu-item-label">{label}</span>
        {sub && <span className="model-menu-item-sub">{sub}</span>}
      </span>
      {selected && <CheckIcon />}
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
