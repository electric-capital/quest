// ModelSelector's two presentations: the desktop popover with its "All
// models" flyout, and the phone full-screen sheet that replaces it -- one
// flat list, keyboard dismissed while open, focus handed back on close.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { AppModelInfo } from '../api/types';
import { getKnownModels, setModelCatalog } from '../constants/models';
import { GRACE_MS } from '../hooks/useFlyoutGrace';
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

// jsdom lays nothing out: give an element the screen box a test needs.
function placeAt(el: Element, box: { left: number; top: number; right: number; bottom: number }) {
  vi.spyOn(el, 'getBoundingClientRect').mockReturnValue({
    ...box, x: box.left, y: box.top, width: box.right - box.left, height: box.bottom - box.top,
    toJSON: () => box,
  } as DOMRect);
}

// Opens "All models" and hovers Claude Opus, whose thinking flyout is placed
// at left 300..450, top 0..100. Returns the rows and the flyout.
function openOpusFlyout() {
  fireEvent.click(trigger());
  fireEvent.click(within(screen.getAllByRole('menu')[0]).getByText('All models'));
  const submenu = screen.getAllByRole('menu')[1];
  const rowOf = (name: string) => within(submenu).getByText(name).closest('.model-effort-anchor') as HTMLElement;
  fireEvent.mouseEnter(rowOf('Claude Opus'));
  const flyout = screen.getByRole('menu', { name: 'Thinking level for Claude Opus' });
  placeAt(flyout, { left: 300, top: 0, right: 450, bottom: 100 });
  return { submenu, rowOf, flyout };
}

const opusFlyout = () => screen.queryByRole('menu', { name: 'Thinking level for Claude Opus' });

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

    it('places the thinking flyout outside the list, clear of its scrollbar', () => {
      renderSelector();
      fireEvent.click(trigger());
      fireEvent.click(within(screen.getAllByRole('menu')[0]).getByText('All models'));
      const submenu = screen.getAllByRole('menu')[1];
      const opusRow = within(submenu).getByText('Claude Opus').closest('.model-effort-anchor') as HTMLElement;
      // The row ends where the list's content does; the list's outer edge
      // (scrollbar, padding, border) is further right.
      placeAt(submenu, { left: 100, top: 0, right: 390, bottom: 320 });
      placeAt(opusRow, { left: 105, top: 260, right: 370, bottom: 300 });
      fireEvent.mouseEnter(opusRow);
      const flyout = screen.getByRole('menu', { name: 'Thinking level for Claude Opus' });
      expect(flyout.style.left).toBe('390px');
      expect(flyout.style.bottom).toBe(`${window.innerHeight - 300}px`);
    });

    it('keeps the thinking flyout while the pointer crosses other rows on the way to it', () => {
      renderSelector();
      const { rowOf } = openOpusFlyout();
      // Up and to the right from Opus, across Sonnet: inside the triangle
      // between the exit point and the flyout.
      fireEvent.mouseLeave(rowOf('Claude Opus'), { clientX: 100, clientY: 90, relatedTarget: rowOf('Claude Sonnet') });
      fireEvent.mouseMove(rowOf('Claude Sonnet'), { clientX: 140, clientY: 80 });
      expect(opusFlyout()).not.toBeNull();
      // Reaching the flyout ends the trip: later moves cannot close it.
      fireEvent.mouseEnter(rowOf('Claude Opus'), { clientX: 310, clientY: 60 });
      fireEvent.mouseMove(document.body, { clientX: 20, clientY: 400 });
      expect(opusFlyout()).not.toBeNull();
    });

    it('applies the held-back hover once the pointer leaves the triangle', () => {
      renderSelector();
      const { rowOf } = openOpusFlyout();
      fireEvent.mouseLeave(rowOf('Claude Opus'), { clientX: 100, clientY: 90, relatedTarget: rowOf('Claude Sonnet') });
      expect(opusFlyout()).not.toBeNull();
      // Straight up instead of toward the flyout: Sonnet (no levels) takes over.
      fireEvent.mouseMove(rowOf('Claude Sonnet'), { clientX: 100, clientY: 40 });
      expect(opusFlyout()).toBeNull();
    });

    it('gives up on a pointer that rests on another row', () => {
      vi.useFakeTimers();
      try {
        renderSelector();
        const { rowOf } = openOpusFlyout();
        fireEvent.mouseLeave(rowOf('Claude Opus'), { clientX: 100, clientY: 90, relatedTarget: rowOf('Claude Sonnet') });
        act(() => vi.advanceTimersByTime(GRACE_MS - 1));
        expect(opusFlyout()).not.toBeNull();
        act(() => vi.advanceTimersByTime(1));
        expect(opusFlyout()).toBeNull();
      } finally {
        vi.useRealTimers();
      }
    });

    it('keeps "All models" open while the pointer crosses the top-level picks toward it', () => {
      renderSelector();
      fireEvent.click(trigger());
      const menu = screen.getAllByRole('menu')[0];
      const allModels = within(menu).getByText('All models').closest('.model-submenu-anchor') as HTMLElement;
      fireEvent.mouseEnter(allModels);
      placeAt(screen.getAllByRole('menu')[1], { left: 300, top: 0, right: 500, bottom: 320 });
      const pick = within(menu).getByText('Smart ($$$)').closest('button') as HTMLElement;
      fireEvent.mouseLeave(allModels, { clientX: 100, clientY: 300, relatedTarget: pick });
      expect(screen.getAllByRole('menu')).toHaveLength(2);
      // Off the way to the list, the pick's hover applies: the list closes.
      fireEvent.mouseMove(pick, { clientX: 60, clientY: 300 });
      expect(screen.getAllByRole('menu')).toHaveLength(1);
    });

    it('scrolls the selection into view once per opening of "All models"', () => {
      renderSelector({ selectedModel: 'opus' });
      fireEvent.click(trigger());
      fireEvent.click(within(screen.getAllByRole('menu')[0]).getByText('All models'));
      expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1);
      // Hovering re-renders the menu; a scrolled list must stay where it is.
      const submenu = screen.getAllByRole('menu')[1];
      fireEvent.mouseEnter(within(submenu).getByText('Claude Sonnet').closest('.model-effort-anchor') as HTMLElement);
      fireEvent.mouseEnter(within(submenu).getByText('Claude Opus').closest('.model-effort-anchor') as HTMLElement);
      expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1);
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
