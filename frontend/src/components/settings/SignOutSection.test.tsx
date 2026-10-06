// SignOutSection: a successful logout drops every unsaved Quest Docs draft
// from this browser (utils/docDraftBackup); a failed one keeps them.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SignOutSection } from './SignOutSection';

const mocks = vi.hoisted(() => ({
  logout: vi.fn(),
}));

vi.mock('../../api/client', () => ({
  logout: mocks.logout,
  logoutAndDisconnect: vi.fn(),
  deleteAccount: vi.fn(),
}));

const KEY = 'quest_doc_draft:me%40example.com:d1';
const DRAFT = JSON.stringify({ content: 'Mine', base: 'T', saved_at: '2026-10-06T10:00:00Z' });

describe('SignOutSection', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('clears the doc drafts after a successful logout', async () => {
    mocks.logout.mockResolvedValue({ success: true });
    localStorage.setItem(KEY, DRAFT);
    render(<SignOutSection />);
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));
    await waitFor(() => expect(localStorage.getItem(KEY)).toBeNull());
  });

  it('keeps them when the logout fails', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    mocks.logout.mockRejectedValue(new Error('offline'));
    localStorage.setItem(KEY, DRAFT);
    render(<SignOutSection />);
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));
    await waitFor(() => expect(mocks.logout).toHaveBeenCalled());
    await waitFor(() =>
      expect((screen.getByRole('button', { name: 'Logout' }) as HTMLButtonElement).disabled).toBe(false),
    );
    expect(localStorage.getItem(KEY)).toBe(DRAFT);
  });
});
