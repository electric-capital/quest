// useSuggestionListFocus on a small list shaped like MemorySuggestionRow's
// (rows with data-suggestion-id, a text field and the action buttons, plus
// an Accept all above them): a row that leaves hands focus to the first
// following row still listed (also when several leave at once), else the
// previous one, else the fallback; a row dropped while its text field had
// focus, without going busy, sends focus to the fallback; a row that stays
// gets focus back once it is enabled again; a non-row control that leaves
// hands focus to the first row; after a tap the target is the row's action
// button; a target that does not take focus passes to the next one, and
// none taking it leaves the next commit to retry; focus elsewhere on the
// page, lost from a control that is still there, or followed by a press
// outside the list is never moved. A browser drops focus to <body> when
// the focused button becomes disabled; jsdom keeps it there and cannot
// blur a disabled element, so `drop()` blurs it just before the commit
// that disables it, and `fixUp()` reproduces the browser's order (disabled
// while focused, then dropped).
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useRef } from 'react';
import { useSuggestionListFocus } from './useSuggestionListFocus';

interface ListProps {
  rows: string[];
  busy?: string[];
  bulk?: 'enabled' | 'disabled' | 'hidden';
  /** Render the fallback heading outside the container. */
  fallbackOutside?: boolean;
}

function List({ rows, busy = [], bulk = 'hidden', fallbackOutside = false }: ListProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const fallbackRef = useRef<HTMLHeadingElement>(null);
  useSuggestionListFocus({ containerRef, rowIds: rows, fallbackRef });
  const heading = <h3 ref={fallbackRef} tabIndex={-1}>Suggestions</h3>;
  return (
    <>
      {fallbackOutside && heading}
      <div ref={containerRef}>
        {!fallbackOutside && heading}
        {bulk !== 'hidden' && (
          <button type="button" disabled={bulk === 'disabled'}>Accept all</button>
        )}
        {rows.map((id) => (
          <div key={id} data-suggestion-id={id}>
            <textarea
              className="memory-suggestion-content"
              aria-label={`Text ${id}`}
              disabled={busy.includes(id)}
            />
            <div className="memory-suggestion-actions">
              <button type="button" disabled={busy.includes(id)}>Accept {id}</button>
              <button type="button" disabled={busy.includes(id)}>Dismiss {id}</button>
            </div>
          </div>
        ))}
      </div>
      <textarea aria-label="Composer" />
      <p>Elsewhere</p>
    </>
  );
}

function focus(el: HTMLElement) {
  act(() => el.focus());
}

/** What a browser does to a focused button as it becomes disabled. */
function drop() {
  act(() => (document.activeElement as HTMLElement).blur());
  expect(document.activeElement).toBe(document.body);
}

/**
 * The browser's focus fix-up: the focused button was disabled by the last
 * commit and focus drops to <body> (jsdom cannot blur a disabled element,
 * so it is enabled for the blur).
 */
function fixUp() {
  const el = document.activeElement as HTMLButtonElement;
  expect(el.disabled).toBe(true);
  act(() => {
    el.disabled = false;
    el.blur();
    el.disabled = true;
  });
  expect(document.activeElement).toBe(document.body);
}

/** A pointer press (jsdom has no PointerEvent, so the type is set by hand). */
function pointerDown(el: Element, pointerType: 'mouse' | 'touch' | 'pen') {
  const event = new Event('pointerdown', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'pointerType', { value: pointerType });
  act(() => {
    el.dispatchEvent(event);
  });
}

const button = (name: string) => screen.getByRole('button', { name });
const field = (id: string) => screen.getByLabelText(`Text ${id}`);

afterEach(cleanup);

describe('useSuggestionListFocus', () => {
  it("moves focus to the next row's field when the focused row leaves", () => {
    const { rerender } = render(<List rows={['a', 'b', 'c']} />);
    focus(button('Accept b'));
    drop();
    rerender(<List rows={['a', 'b', 'c']} busy={['b']} />);
    rerender(<List rows={['a', 'c']} />);
    expect(document.activeElement).toBe(field('c'));
  });

  it('also when focus stayed on the busy button until the row left', () => {
    const { rerender } = render(<List rows={['a', 'b', 'c']} />);
    focus(button('Dismiss a'));
    rerender(<List rows={['a', 'b', 'c']} busy={['a']} />);
    rerender(<List rows={['b', 'c']} />);
    expect(document.activeElement).toBe(field('b'));
  });

  it('moves focus to the previous row when the last row leaves', () => {
    const { rerender } = render(<List rows={['a', 'b']} />);
    focus(button('Dismiss b'));
    drop();
    rerender(<List rows={['a', 'b']} busy={['b']} />);
    rerender(<List rows={['a']} />);
    expect(document.activeElement).toBe(field('a'));
  });

  it('follows the row as rows above it leave, then takes the row at its place', () => {
    const { rerender } = render(<List rows={['a', 'b', 'c', 'd', 'e']} />);
    focus(button('Accept c'));
    drop();
    rerender(<List rows={['c', 'd', 'e']} busy={['c']} />);
    rerender(<List rows={['d', 'e']} />);
    expect(document.activeElement).toBe(field('d'));
  });

  it('moves focus to the fallback when the only row leaves, inside or outside the container', () => {
    for (const fallbackOutside of [false, true]) {
      const { rerender, unmount } = render(<List rows={['a']} fallbackOutside={fallbackOutside} />);
      focus(button('Accept a'));
      drop();
      rerender(<List rows={['a']} busy={['a']} fallbackOutside={fallbackOutside} />);
      rerender(<List rows={[]} fallbackOutside={fallbackOutside} />);
      expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Suggestions' }));
      unmount();
    }
  });

  it('waits while the row is busy, then gives focus back to its re-enabled button', () => {
    const { rerender } = render(<List rows={['a', 'b']} />);
    focus(button('Accept a'));
    drop();
    rerender(<List rows={['a', 'b']} busy={['a']} />);
    // Another commit while the row is still busy: nothing to focus yet.
    rerender(<List rows={['a', 'b']} busy={['a']} />);
    expect(document.activeElement).toBe(document.body);
    // The accept failed: the row stays with its controls enabled again.
    rerender(<List rows={['a', 'b']} />);
    expect(document.activeElement).toBe(button('Accept a'));
  });

  it('never moves focus that is elsewhere on the page', () => {
    const { rerender } = render(<List rows={['a', 'b', 'c']} />);
    const composer = screen.getByLabelText('Composer');
    // Tabbed away while the action ran.
    focus(button('Accept a'));
    focus(composer);
    rerender(<List rows={['a', 'b', 'c']} busy={['a']} />);
    rerender(<List rows={['b', 'c']} />);
    expect(document.activeElement).toBe(composer);

    // Focus dropped by the busy button, then the user went to the composer.
    focus(button('Accept b'));
    drop();
    rerender(<List rows={['b', 'c']} busy={['b']} />);
    focus(composer);
    rerender(<List rows={['c']} />);
    expect(document.activeElement).toBe(composer);
  });

  it('leaves focus lost from a control that is still there (a click on a blank area)', () => {
    const { rerender } = render(<List rows={['a', 'b']} />);
    focus(button('Dismiss b'));
    act(() => button('Dismiss b').blur());
    rerender(<List rows={['b']} />);
    expect(document.activeElement).toBe(document.body);
  });

  it('sends focus from a non-row control that leaves to the first row', () => {
    const { rerender } = render(<List rows={['a', 'b', 'c']} bulk="enabled" />);
    focus(button('Accept all'));
    drop();
    rerender(<List rows={['a', 'b', 'c']} bulk="disabled" />);
    rerender(<List rows={['b', 'c']} bulk="disabled" busy={['b']} />);
    expect(document.activeElement).toBe(document.body);
    // Two rows were skipped and stay; Accept all goes with the third.
    rerender(<List rows={['b', 'c']} bulk="hidden" />);
    expect(document.activeElement).toBe(field('b'));
  });

  it('gives focus back to a non-row control that is enabled again', () => {
    const { rerender } = render(<List rows={['a', 'b', 'c']} bulk="enabled" />);
    focus(button('Accept all'));
    drop();
    rerender(<List rows={['a', 'b', 'c']} bulk="disabled" />);
    rerender(<List rows={['b', 'c']} bulk="enabled" />);
    expect(document.activeElement).toBe(button('Accept all'));
  });

  it('falls back when a non-row control leaves with no row left', () => {
    const { rerender } = render(<List rows={['a', 'b']} bulk="enabled" />);
    focus(button('Accept all'));
    rerender(<List rows={[]} bulk="hidden" />);
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Suggestions' }));
  });

  it("follows the browser's order: disabled while focused, dropped, then enabled again", () => {
    const { rerender } = render(<List rows={['a', 'b']} />);
    focus(button('Accept a'));
    rerender(<List rows={['a', 'b']} busy={['a']} />);
    fixUp();
    rerender(<List rows={['a', 'b']} busy={['a']} />);
    expect(document.activeElement).toBe(document.body);
    rerender(<List rows={['a', 'b']} />);
    expect(document.activeElement).toBe(button('Accept a'));

    // And when the row leaves instead.
    focus(button('Accept a'));
    rerender(<List rows={['a', 'b']} busy={['a']} />);
    fixUp();
    rerender(<List rows={['b']} />);
    expect(document.activeElement).toBe(field('b'));
  });

  it('leaves focus in a text field when a re-read drops a row above it', () => {
    const { rerender } = render(<List rows={['a', 'b', 'c']} />);
    focus(field('c'));
    rerender(<List rows={['b', 'c']} />);
    expect(document.activeElement).toBe(field('c'));
  });

  it('moves focus to the first following row still listed when several rows leave at once', () => {
    const { rerender } = render(<List rows={['a', 'b', 'c', 'd']} />);
    focus(button('Accept b'));
    drop();
    rerender(<List rows={['a', 'b', 'c', 'd']} busy={['b']} />);
    // A re-read lands with a and b gone together.
    rerender(<List rows={['c', 'd']} />);
    expect(document.activeElement).toBe(field('c'));
  });

  it('sends focus to the fallback when the row being typed in is dropped without going busy', () => {
    const { rerender } = render(<List rows={['a', 'b', 'c']} />);
    focus(field('b'));
    // Resolved in another tab: a re-read drops the row under the cursor.
    rerender(<List rows={['a', 'c']} />);
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Suggestions' }));
  });

  it("targets the next row's action button, not its text field, after a tap", () => {
    for (const pointerType of ['touch', 'pen'] as const) {
      const { rerender, unmount } = render(<List rows={['a', 'b']} />);
      pointerDown(button('Accept a'), pointerType);
      focus(button('Accept a'));
      drop();
      rerender(<List rows={['a', 'b']} busy={['a']} />);
      rerender(<List rows={['b']} />);
      expect(document.activeElement).toBe(button('Accept b'));
      unmount();
    }
  });

  it('keeps the text field for a mouse click, or a key after a tap', () => {
    const { rerender } = render(<List rows={['a', 'b', 'c']} />);
    pointerDown(button('Accept a'), 'mouse');
    focus(button('Accept a'));
    drop();
    rerender(<List rows={['a', 'b', 'c']} busy={['a']} />);
    rerender(<List rows={['b', 'c']} />);
    expect(document.activeElement).toBe(field('b'));

    pointerDown(button('Accept b'), 'touch');
    focus(button('Accept b'));
    fireEvent.keyDown(button('Accept b'), { key: 'Enter' });
    drop();
    rerender(<List rows={['b', 'c']} busy={['b']} />);
    rerender(<List rows={['c']} />);
    expect(document.activeElement).toBe(field('c'));
  });

  it('does not move focus after a press outside the list while the action ran', () => {
    const { rerender } = render(<List rows={['a', 'b']} />);
    focus(button('Accept a'));
    drop();
    rerender(<List rows={['a', 'b']} busy={['a']} />);
    // The user clicks (or selects) text elsewhere: no focusin, focus stays on <body>.
    pointerDown(screen.getByText('Elsewhere'), 'mouse');
    rerender(<List rows={['b']} />);
    expect(document.activeElement).toBe(document.body);
  });

  it('keeps the list after a press inside it', () => {
    const { rerender, container } = render(<List rows={['a', 'b']} />);
    focus(button('Accept a'));
    drop();
    rerender(<List rows={['a', 'b']} busy={['a']} />);
    pointerDown(container.querySelector('[data-suggestion-id="b"]')!, 'mouse');
    rerender(<List rows={['b']} />);
    expect(document.activeElement).toBe(field('b'));
  });

  it('passes a target that does not take focus to the next, and retries later when none does', () => {
    const { rerender } = render(<List rows={['a', 'b']} />);
    // The next row's field cannot take focus: the fallback gets it.
    const hiddenField = vi.spyOn(field('b'), 'focus').mockImplementation(() => {});
    focus(button('Accept a'));
    drop();
    rerender(<List rows={['a', 'b']} busy={['a']} />);
    rerender(<List rows={['b']} />);
    const heading = screen.getByRole('heading', { name: 'Suggestions' });
    expect(document.activeElement).toBe(heading);
    hiddenField.mockRestore();

    // A hidden fallback (a phone-only heading on a wide screen) takes nothing...
    const hiddenHeading = vi.spyOn(heading, 'focus').mockImplementation(() => {});
    focus(button('Dismiss b'));
    drop();
    rerender(<List rows={['b']} busy={['b']} />);
    rerender(<List rows={[]} />);
    expect(document.activeElement).toBe(document.body);
    // ...and a later commit, once it can take focus, tries again.
    hiddenHeading.mockRestore();
    rerender(<List rows={[]} />);
    expect(document.activeElement).toBe(heading);
  });
});
