// AutoProjectOutline, the AutoProject page's table of contents:
//
// - scanAutoProjectOutline reads the column as rendered: one entry per card,
//   the brief's h1-h3 indented from its shallowest level, at most ten
//   updates in the order shown, nothing for an absent card.
// - The component lists them, follows changes of the column
//   (MutationObserver) and scrolls the scroller to an entry's target.
//
// jsdom has no layout: positions come from stubbed getBoundingClientRect.
import { createRef } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AutoProjectOutline, OUTLINE_MAX_UPDATES, scanAutoProjectOutline } from './AutoProjectOutline';

function updateHtml(seq: number, prompt: string): string {
  return `<article class="autoproject-run" data-run-id="r${seq}">
    <div class="autoproject-run-meta"><span class="autoproject-run-seq">Update ${seq}</span></div>
    <div class="autoproject-run-request"><p>You asked</p><div class="autoproject-run-request-text">${prompt}</div></div>
  </article>`;
}

function buildColumn(opts: { brief?: string; updates?: string; artifacts?: boolean } = {}): HTMLDivElement {
  const column = document.createElement('div');
  column.innerHTML = `
    <section class="autoproject-card autoproject-brief">
      <h2 class="autoproject-card-title">Brief</h2>
      <div class="autoproject-brief-body">${opts.brief ?? ''}</div>
    </section>
    <section class="autoproject-card autoproject-files"><h2 class="autoproject-card-title">AutoProject files</h2></section>
    ${opts.artifacts ? '<section class="autoproject-card autoproject-artifacts"><h2 class="autoproject-card-title">Docs and tables</h2></section>' : ''}
    <div class="autoproject-composer-block"></div>
    <section class="autoproject-card autoproject-updates">
      <h2 class="autoproject-card-title">Updates</h2>
      <div class="autoproject-updates-list">${opts.updates ?? ''}</div>
    </section>`;
  return column;
}

afterEach(() => {
  cleanup();
  document.body.innerHTML = '';
});

describe('scanAutoProjectOutline', () => {
  it('lists the cards, the brief headings and the updates', () => {
    const column = buildColumn({
      brief: '<h2>Goal</h2><p>text</p><h3>Scope</h3><h4>Too deep</h4><h2>  Open   questions </h2><h2></h2>',
      updates: updateHtml(3, 'Third prompt') + updateHtml(2, 'Second prompt'),
      artifacts: true,
    });
    const items = scanAutoProjectOutline(column);
    expect(items.map((i) => [i.depth, i.prefix ?? '', i.label])).toEqual([
      [0, '', 'Brief'],
      [1, '', 'Goal'],
      [2, '', 'Scope'],
      [1, '', 'Open questions'],
      [0, '', 'Files'],
      [0, '', 'Docs and tables'],
      [0, '', 'Updates'],
      [1, '3', 'Third prompt'],
      [1, '2', 'Second prompt'],
    ]);
  });

  it('skips an absent card and caps the updates at the first ten shown', () => {
    const updates = Array.from({ length: 14 }, (_, i) => updateHtml(14 - i, `Prompt ${14 - i}`)).join('');
    const items = scanAutoProjectOutline(buildColumn({ updates }));
    expect(items.some((i) => i.label === 'Docs and tables')).toBe(false);
    const listed = items.filter((i) => i.key.startsWith('update:'));
    expect(listed).toHaveLength(OUTLINE_MAX_UPDATES);
    expect(listed[0].prefix).toBe('14');
    expect(listed[listed.length - 1].prefix).toBe('5');
  });

  it('labels an update without a prompt (the pending entry) by its header', () => {
    const column = buildColumn({
      updates:
        '<article class="autoproject-run pending"><div class="autoproject-run-meta"><span class="autoproject-run-seq">New update</span></div></article>',
    });
    const item = scanAutoProjectOutline(column).find((i) => i.key.startsWith('update:'));
    expect(item?.label).toBe('New update');
    expect(item?.prefix).toBeUndefined();
  });
});

describe('AutoProjectOutline', () => {
  function mount(column: HTMLDivElement) {
    const scroller = document.createElement('div');
    scroller.appendChild(column);
    document.body.appendChild(scroller);
    scroller.scrollTo = vi.fn();
    const scrollerRef = createRef<HTMLElement>();
    const columnRef = createRef<HTMLElement>();
    scrollerRef.current = scroller;
    columnRef.current = column;
    render(<AutoProjectOutline scrollerRef={scrollerRef} columnRef={columnRef} />);
    return scroller;
  }

  it('renders the entries and follows the column', async () => {
    const column = buildColumn({ brief: '<h2>Goal</h2>' });
    mount(column);
    const nav = screen.getByRole('navigation', { name: 'On this page' });
    expect(nav.textContent).toBe('BriefGoalFilesUpdates');

    await act(async () => {
      column.querySelector('.autoproject-updates-list')!.innerHTML = updateHtml(1, 'First prompt');
    });
    await waitFor(() => expect(screen.getByRole('button', { name: /First prompt/ })).toBeTruthy());
  });

  it('scrolls the scroller to the entry target', () => {
    const column = buildColumn({ brief: '<h2>Goal</h2>' });
    const scroller = mount(column);
    scroller.scrollTop = 100;
    scroller.getBoundingClientRect = () => ({ top: 50 }) as DOMRect;
    column.querySelector('h2:not(.autoproject-card-title)')!.getBoundingClientRect = () => ({ top: 400 }) as DOMRect;
    fireEvent.click(screen.getByRole('button', { name: 'Goal' }));
    // 400 - 50 + 100 = 450 in content coordinates, less the 12px margin.
    expect(scroller.scrollTo).toHaveBeenCalledWith(expect.objectContaining({ top: 438 }));
  });
});
