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
 * Interaction mirrors the Flags popover (opens upward, outside-click close)
 * plus Escape-to-close and hover-or-click to open the submenu.
 */

import { useEffect, useRef, useState, useCallback } from 'react';
import {
  getModelDisplayName, getModelInfo, getTopLevelModels, isDeprecatedModel, isModelAllowedFor,
} from '../constants/models';
import type { ModelInfo, ModelVisibility } from '../constants/models';

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
}

export function ModelSelector({
  selectedModel,
  models,
  onSelect,
  disabled = false,
  labelOverride,
  visibility = 'private',
}: ModelSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [isSubmenuOpen, setIsSubmenuOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => {
    setIsOpen(false);
    setIsSubmenuOpen(false);
  }, []);

  // Outside-click close (Flags popover pattern).
  useEffect(() => {
    if (!isOpen) return;
    const handleClickOutside = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        close();
      }
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

  return (
    <div className="model-menu-container" ref={containerRef}>
      <button
        type="button"
        id="model-select"
        className="model-menu-trigger"
        onClick={() => setIsOpen((o) => !o)}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        title="Choose the model for this conversation"
      >
        <span className="model-menu-trigger-label">{triggerLabel}</span>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="18 15 12 9 6 15"></polyline>
        </svg>
      </button>
      {isOpen && (
        <div className="model-menu" role="menu">
          {topLevel.map((pick) => (
            <button
              type="button"
              key={pick.id}
              role="menuitem"
              className={
                'model-menu-item'
                + (pick.id === selectedModel ? ' model-menu-item-selected' : '')
              }
              onClick={() => handlePick(pick.id)}
              onMouseEnter={() => setIsSubmenuOpen(false)}
            >
              <span className="model-menu-item-text">
                <span className="model-menu-item-label">{pick.descriptor || pick.name}</span>
                {pick.descriptor && <span className="model-menu-item-sub">{pick.name}</span>}
              </span>
              {pick.id === selectedModel && <CheckIcon />}
            </button>
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
                {selectionMissing && (
                  <button
                    type="button"
                    role="menuitem"
                    className="model-menu-item model-menu-item-selected"
                    onClick={() => handlePick(selectedModel)}
                  >
                    <span className="model-menu-item-text">
                      <span className="model-menu-item-label">
                        {getModelDisplayName(selectedModel) + missingSuffix}
                      </span>
                    </span>
                    <CheckIcon />
                  </button>
                )}
                {models.map((model) => (
                  <button
                    type="button"
                    key={model.id}
                    role="menuitem"
                    className={
                      'model-menu-item'
                      + (model.id === selectedModel ? ' model-menu-item-selected' : '')
                    }
                    // The submenu scrolls when the model list outgrows its
                    // max-height; bring the current selection into view on
                    // open so it isn't hidden below the fold.
                    ref={model.id === selectedModel
                      ? (el) => el?.scrollIntoView({ block: 'nearest' })
                      : undefined}
                    onClick={() => handlePick(model.id)}
                  >
                    <span className="model-menu-item-text">
                      <span className="model-menu-item-label">{model.name}</span>
                    </span>
                    {model.id === selectedModel && <CheckIcon />}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function CheckIcon() {
  return (
    <svg className="model-menu-check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12"></polyline>
    </svg>
  );
}
