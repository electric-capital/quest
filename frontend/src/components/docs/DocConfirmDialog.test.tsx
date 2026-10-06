// DocConfirmDialog: named by its title, focuses Cancel on open (unless a
// child already took focus), returns focus to the opener on close, and
// cannot be closed while busy.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { DocConfirmDialog } from './DocConfirmDialog';

function Harness({ busy = false, autoFocusChild = false }: { busy?: boolean; autoFocusChild?: boolean }) {
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
        error={null}
        onConfirm={vi.fn()}
        onClose={() => setOpen(false)}
      >
        {autoFocusChild ? <input aria-label="Title" autoFocus /> : <p>This cannot be undone.</p>}
      </DocConfirmDialog>
    </>
  );
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
});
