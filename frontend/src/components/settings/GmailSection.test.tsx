// GmailSection: the Save button's states, through the shared useSaveStatus
// hook and SaveActions row.
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GmailSection } from './GmailSection';

const fetchSettings = vi.fn();
const updateSettings = vi.fn();

vi.mock('../../api/client', () => ({
  fetchSettings: (...args: unknown[]) => fetchSettings(...args),
  updateSettings: (...args: unknown[]) => updateSettings(...args),
}));

beforeEach(() => {
  fetchSettings.mockResolvedValue({ settings: { gmail_labels: ['receipts'] } });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe('GmailSection save', () => {
  it('saves the labels and shows Saved', async () => {
    let finish: () => void = () => {};
    updateSettings.mockReturnValue(new Promise<void>((resolve) => { finish = resolve; }));
    render(<GmailSection />);

    const save = await screen.findByRole('button', { name: 'Save' });
    fireEvent.click(save);

    const saving = await screen.findByRole('button', { name: 'Saving...' });
    expect(saving).toHaveProperty('disabled', true);
    expect(updateSettings).toHaveBeenCalledWith({ gmail_labels: ['receipts'] });

    finish();
    const note = await screen.findByText('Saved');
    expect(note.className).toBe('settings-save-status saved');
    expect(screen.getByRole('button', { name: 'Save' })).toHaveProperty('disabled', false);
  });

  it('shows the failure message when the save is rejected', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    updateSettings.mockRejectedValue(new Error('Too many labels'));
    render(<GmailSection />);

    fireEvent.click(await screen.findByRole('button', { name: 'Save' }));

    const note = await screen.findByText('Too many labels');
    expect(note.className).toBe('settings-save-status error');
  });
});
