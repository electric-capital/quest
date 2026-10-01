/**
 * The "Conversation options" popover on a Sidebar conversation section
 * header: a vertical-dots button toggling a checkbox list of visibility
 * filters. Closes on any outside click.
 *
 * Controlled: the owning section holds `open`, because toggling a filter
 * triggers a (non-silent) reload that swaps the header for the loading
 * placeholder -- the popover must come back open once the rows land, so its
 * state has to outlive this component.
 */

import { useEffect } from 'react';
import { MoreVertical } from 'lucide-react';

export interface ConversationFilterOption {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}

interface ConversationFilterMenuProps {
  options: ConversationFilterOption[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function ConversationFilterMenu({ options, open, onOpenChange }: ConversationFilterMenuProps) {
  // Close the popover on click outside
  useEffect(() => {
    if (!open) return;
    const handleClickOutside = () => onOpenChange(false);
    document.addEventListener('click', handleClickOutside);
    return () => document.removeEventListener('click', handleClickOutside);
  }, [open, onOpenChange]);

  return (
    <div className="conversation-filter-wrapper">
      <button
        className="conversation-filter-button"
        onClick={(e) => {
          e.stopPropagation();
          onOpenChange(!open);
        }}
        title="Conversation options"
        aria-label="Conversation options"
      >
        <MoreVertical size={14} />
      </button>
      {open && (
        <div
          className="conversation-filter-menu"
          onClick={(e) => e.stopPropagation()}
        >
          {options.map((option) => (
            <label key={option.label} className="conversation-filter-menu-item">
              <input
                type="checkbox"
                checked={option.checked}
                onChange={(e) => option.onChange(e.target.checked)}
              />
              <span>{option.label}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
