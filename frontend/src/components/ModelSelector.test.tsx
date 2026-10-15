// ModelSelector's two presentations: the desktop popover with its "All
// models" flyout, and the phone full-screen sheet that replaces it -- one
// flat list, keyboard dismissed while open, focus handed back on close.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { AppModelInfo } from '../api/types';
import { getKnownModels, setModelCatalog } from '../constants/models';
import { ModelSelector } from './ModelSelector';

const mocks = vi.hoisted(() => ({ isMobile: false }));

vi.mock('../hooks/useIsMobile', () => ({
  useIsMobile: () => mocks.isMobile,
}));

// Catalog entries as GET /app/api/config delivers them.
function model(
  id: string, name: string, slot: number | null = null, descriptor = '',
  extra: Partial<AppModelInfo> = {},
): AppModelInfo {
  return {
    id,
    display_name: name,
    provider: 'anthropic',
    provider_label: 'Anthropic',
    max_input_tokens: 200000,
    deprecated: false,
    thinking_efforts: [],
    default_thinking_effort: null,
    slot,
    public_slot: null,
    descriptor,
    slot_effort: null,
    allow_private: true,
    allow_public: true,
    ...extra,
  };
}

const CATALOG = [
  // The slotted pick is fixed to "high"; under "All models" Opus offers
  // its levels as a sublevel.
  model('opus', 'Claude Opus', 1, 'Smart ($$$)', {
    thinking_efforts: ['low', 'medium', 'high', 'max'], default_thinking_effort: 'medium', slot_effort: 'high',
  }),
  model('sonnet', 'Claude Sonnet', 2),
  model('haiku', 'Claude Haiku'),
];

function renderSelector(props: Partial<React.ComponentProps<typeof ModelSelector>> = {}) {
  const onSelect = vi.fn();
  const onOpenChange = vi.fn();
  render(
    <div>
      <textarea aria-label="Message" />
      <ModelSelector
        selectedModel="haiku"
        models={getKnownModels()}
        onSelect={onSelect}
        onOpenChange={onOpenChange}
        {...props}
      />
    </div>,
  );
  return { onSelect, onOpenChange };
}

const trigger = () => screen.getByTitle('Choose the model for this conversation');

describe('ModelSelector', () => {
  beforeEach(() => {
    mocks.isMobile = false;
    setModelCatalog(CATALOG);
    // jsdom does not implement scrollIntoView.
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    cleanup();
  });

  describe('desktop', () => {
    it('opens a popover whose "All models" row reveals the full list', () => {
      const { onSelect } = renderSelector();
      fireEvent.click(trigger());

      expect(screen.queryByRole('dialog')).toBeNull();
      const menu = screen.getAllByRole('menu')[0];
      // Top level: the slotted picks only, descriptor over model name.
      expect(within(menu).getByText('Smart ($$$)')).toBeTruthy();
      expect(within(menu).queryByText('Claude Haiku')).toBeNull();

      fireEvent.click(within(menu).getByText('All models'));
      const submenu = screen.getAllByRole('menu')[1];
      fireEvent.click(within(submenu).getByText('Claude Haiku'));
      // A model without levels is picked at its (only) default level.
      expect(onSelect).toHaveBeenCalledWith('haiku', null);
      expect(screen.queryByRole('menu')).toBeNull();
    });

    it('fixes a slotted pick to its admin-set thinking level', () => {
      const { onSelect } = renderSelector();
      fireEvent.click(trigger());
      const menu = screen.getAllByRole('menu')[0];
      // The sublabel names the model and the level the pick runs at.
      expect(within(menu).getByText('Claude Opus · High')).toBeTruthy();
      fireEvent.click(within(menu).getByText('Smart ($$$)'));
      expect(onSelect).toHaveBeenCalledWith('opus', 'high');
    });

    it('offers a model\'s thinking levels as a sublevel under "All models"', () => {
      const { onSelect } = renderSelector({ selectedModel: 'opus', selectedThinkingEffort: 'max' });
      // The trigger shows the picked level after the model name.
      expect(trigger().textContent).toContain('Claude Opus · Max');
      fireEvent.click(trigger());
      fireEvent.click(within(screen.getAllByRole('menu')[0]).getByText('All models'));
      const submenu = screen.getAllByRole('menu')[1];
      const opusRow = within(submenu).getByText('Claude Opus').closest('button') as HTMLButtonElement;
      // Hovering the row opens the level flyout; the default is marked.
      fireEvent.mouseEnter(opusRow.parentElement as HTMLElement);
      const flyout = screen.getByRole('menu', { name: 'Thinking level for Claude Opus' });
      expect(within(flyout).getByText('Medium (default)')).toBeTruthy();
      expect(within(flyout).getByText('Max').closest('button')?.getAttribute('aria-current')).toBe('true');
      // Models without levels have no flyout to open.
      expect(within(submenu).getByText('Claude Haiku').closest('button')?.getAttribute('aria-haspopup')).toBeNull();

      fireEvent.click(within(flyout).getByText('Low'));
      expect(onSelect).toHaveBeenCalledWith('opus', 'low');
      expect(screen.queryByRole('menu')).toBeNull();

      // Clicking the model row itself picks the default level.
      fireEvent.click(trigger());
      fireEvent.click(within(screen.getAllByRole('menu')[0]).getByText('All models'));
      fireEvent.click(within(screen.getAllByRole('menu')[1]).getByText('Claude Opus'));
      expect(onSelect).toHaveBeenLastCalledWith('opus', null);
    });
  });

  describe('phone', () => {
    beforeEach(() => {
      mocks.isMobile = true;
    });

    it('opens a full-screen sheet listing the picks and every model at once', () => {
      renderSelector();
      fireEvent.click(trigger());

      const sheet = screen.getByRole('dialog', { name: 'Choose a model' });
      expect(screen.queryByRole('menu')).toBeNull();
      expect(within(sheet).getByText('Smart ($$$)')).toBeTruthy();
      expect(within(sheet).getByText('All models')).toBeTruthy();
      // No drill-in: the full list is already there. The slotted models
      // appear twice (pick row + full list), the unslotted one once.
      expect(within(sheet).getAllByText('Claude Sonnet')).toHaveLength(2);
      expect(within(sheet).getAllByText('Claude Haiku')).toHaveLength(1);
      expect(within(sheet).getByText('Claude Haiku').closest('button')?.getAttribute('aria-current'))
        .toBe('true');
    });

    it('takes focus from the composer while open and returns it on pick', () => {
      const { onSelect, onOpenChange } = renderSelector();
      const textarea = screen.getByLabelText('Message');
      textarea.focus();

      fireEvent.click(trigger());
      const sheet = screen.getByRole('dialog');
      // Focus left the textarea (this is what drops the on-screen keyboard).
      expect(document.activeElement).toBe(sheet);
      expect(onOpenChange).toHaveBeenLastCalledWith(true);

      fireEvent.click(within(sheet).getByText('Smart ($$$)'));
      expect(onSelect).toHaveBeenCalledWith('opus', 'high');
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(document.activeElement).toBe(textarea);
      expect(onOpenChange).toHaveBeenLastCalledWith(false);
    });

    it('lists a model\'s thinking levels as chips under its row', () => {
      const { onSelect } = renderSelector({ selectedModel: 'opus', selectedThinkingEffort: 'low' });
      fireEvent.click(trigger());
      const sheet = screen.getByRole('dialog');
      const chips = within(sheet).getByRole('group', { name: 'Thinking level for Claude Opus' });
      expect(within(chips).getByText('Low').getAttribute('aria-pressed')).toBe('true');
      expect(within(sheet).queryByRole('group', { name: 'Thinking level for Claude Haiku' })).toBeNull();
      fireEvent.click(within(chips).getByText('Medium (default)'));
      expect(onSelect).toHaveBeenCalledWith('opus', 'medium');
    });

    it('closes from the header button and on Escape without selecting', () => {
      const { onSelect } = renderSelector();

      fireEvent.click(trigger());
      fireEvent.click(screen.getByRole('button', { name: 'Close' }));
      expect(screen.queryByRole('dialog')).toBeNull();

      fireEvent.click(trigger());
      fireEvent.keyDown(document, { key: 'Escape' });
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(onSelect).not.toHaveBeenCalled();
    });

    it('stays open for a press inside the sheet', () => {
      renderSelector();
      fireEvent.click(trigger());
      // The sheet is portaled out of the menu container; a press inside it
      // must not count as an outside click.
      fireEvent.mouseDown(screen.getByText('All models'));
      expect(screen.getByRole('dialog')).toBeTruthy();
    });

    it('keeps an unoffered current selection visible in the list', () => {
      renderSelector({ selectedModel: 'retired-model' });
      fireEvent.click(trigger());
      const sheet = screen.getByRole('dialog');
      expect(within(sheet).getByText('retired-model (no credentials)')).toBeTruthy();
    });
  });
});
