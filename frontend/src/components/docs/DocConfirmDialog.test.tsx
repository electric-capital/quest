// DocConfirmDialog: named by its title, focuses Cancel on open (unless a
// child already took focus), returns focus to the opener on close, cannot
// be closed while busy, keeps Tab inside it while open (wrapping at both
// ends, re-entering from outside) and gives focus back after a failed
// confirm. jsdom has no Tab navigation: the trap is driven by dispatching
// the keydown and checking whether it was prevented and where focus went.
// A browser drops focus from a button as it becomes disabled; jsdom keeps
// it there, so `drop()` blurs it just before the busy commit.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { DocConfirmDialog } from './DocConfirmDialog';

interface HarnessProps {
  busy?: boolean;
  autoFocusChild?: boolean;
  error?: string | null;
  confirmDisabled?: boolean;
  secondaryLabel?: string;
}

function Harness({
  busy = false,
  autoFocusChild = false,
  error = null,
  confirmDisabled = false,
  secondaryLabel,
}: HarnessProps) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open
      </button>
      <DocConfirmDialog
        isOpen={open}
        title="Delete 'Plan'?"
        confirmLabel="Delete"
        busy={busy}
        error={error}
        confirmDisabled={confirmDisabled}
        secondaryLabel={secondaryLabel}
        onSecondary={vi.fn()}
        onConfirm={vi.fn()}
        onClose={() => setOpen(false)}
      >
        {autoFocusChild ? <input aria-label="Title" autoFocus /> : <p>This cannot be undone.</p>}
      </DocConfirmDialog>
    </>
  );
}

const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement;

function openDialog() {
  const opener = button('Open');
  act(() => opener.focus());
  fireEvent.click(opener);
  return opener;
}

/** Press Tab (or Shift+Tab) on the focused element; true when the browser may move focus. */
function tab(shiftKey = false): boolean {
  return fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Tab', shiftKey });
}

/** What a browser does to a focused button as it becomes disabled. */
function drop() {
  act(() => (document.activeElement as HTMLElement).blur());
  expect(document.activeElement).toBe(document.body);
}

describe('DocConfirmDialog', () => {
  afterEach(() => cleanup());

  it('is named by its title and focuses Cancel, then returns focus to the opener', () => {
    render(<Harness />);
    const opener = screen.getByRole('button', { name: 'Open' });
    opener.focus();
    fireEvent.click(opener);
    expect(screen.getByRole('dialog', { name: "Delete 'Plan'?" })).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Cancel' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it('leaves focus on a child that autofocused', () => {
    render(<Harness autoFocusChild />);
    fireEvent.click(screen.getByRole('button', { name: 'Open' }));
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Title' }));
  });

  it('cannot be closed while busy', () => {
    render(<Harness busy />);
    fireEvent.click(screen.getByRole('button', { name: 'Open' }));
    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('dialog')).toBeTruthy();
  });

  describe('focus trap', () => {
    it('Tab from the last control wraps to the first, Shift+Tab from the first to the last', () => {
      render(<Harness />);
      openDialog();
      const cancel = button('Cancel');
      const confirm = button('Delete');
      expect(document.activeElement).toBe(cancel);

      // Between the ends the browser moves focus.
      expect(tab()).toBe(true);
      act(() => confirm.focus());
      expect(tab()).toBe(false);
      expect(document.activeElement).toBe(cancel);

      expect(tab(true)).toBe(false);
      expect(document.activeElement).toBe(confirm);
      expect(tab(true)).toBe(true);
      expect(document.activeElement).toBe(confirm);
    });

    it('cycles over the enabled controls only, the body\'s included', () => {
      render(<Harness autoFocusChild secondaryLabel="Download Original..." confirmDisabled />);
      openDialog();
      const input = screen.getByRole('textbox', { name: 'Title' });
      expect(document.activeElement).toBe(input);

      // The disabled confirm is skipped: the secondary action is the last stop.
      expect(tab(true)).toBe(false);
      expect(document.activeElement).toBe(button('Download Original...'));
      expect(tab()).toBe(false);
      expect(document.activeElement).toBe(input);
    });

    it('brings focus back in from outside the dialog', () => {
      render(<Harness />);
      openDialog();
      act(() => button('Cancel').blur());
      expect(tab()).toBe(false);
      expect(document.activeElement).toBe(button('Cancel'));

      act(() => button('Cancel').blur());
      expect(tab(true)).toBe(false);
      expect(document.activeElement).toBe(button('Delete'));
    });

    it('lets Tab go once the dialog is closed', () => {
      render(<Harness />);
      const opener = openDialog();
      fireEvent.click(button('Cancel'));
      expect(document.activeElement).toBe(opener);
      expect(tab()).toBe(true);
      expect(tab(true)).toBe(true);
      expect(document.activeElement).toBe(opener);
    });

    it('holds Tab while busy, with nothing enabled inside', () => {
      render(<Harness busy />);
      openDialog();
      act(() => (document.activeElement as HTMLElement).blur());
      expect(tab()).toBe(false);
      expect(document.activeElement).toBe(document.body);
    });
  });

  describe('after a failed confirm', () => {
    it('gives focus back to the confirm button the browser dropped it from', () => {
      const { rerender } = render(<Harness />);
      openDialog();
      act(() => button('Delete').focus());
      drop();
      rerender(<Harness busy />);
      rerender(<Harness error="Server exploded" />);
      expect(screen.getByRole('alert').textContent).toBe('Server exploded');
      expect(document.activeElement).toBe(button('Delete'));
    });

    it('gives it to Cancel while the confirm stays disabled', () => {
      const { rerender } = render(<Harness />);
      openDialog();
      act(() => button('Delete').focus());
      drop();
      rerender(<Harness busy />);
      rerender(<Harness error="Server exploded" confirmDisabled />);
      expect(document.activeElement).toBe(button('Cancel'));
    });

    it('leaves focus that is still on a control alone', () => {
      const { rerender } = render(<Harness />);
      openDialog();
      const cancel = button('Cancel');
      expect(document.activeElement).toBe(cancel);
      // jsdom keeps focus on the disabled Cancel: nothing was lost.
      rerender(<Harness busy />);
      rerender(<Harness error="Server exploded" />);
      expect(document.activeElement).toBe(cancel);
    });
  });
});
