// Settings > Memories table: rows render with a count, the filter narrows
// the table without refetching, archived rows are only fetched on request,
// creating prepends a row, editing is in-row, and archive drops the row
// from the active view.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { Memory } from '../../api/types';
import { MemoriesSection } from './MemoriesSection';

const mocks = vi.hoisted(() => ({
  fetchMemories: vi.fn(),
  createMemory: vi.fn(),
  updateMemory: vi.fn(),
  archiveMemory: vi.fn(),
  unarchiveMemory: vi.fn(),
  deleteMemory: vi.fn(),
  fetchProjectMemories: vi.fn(),
  createProjectMemory: vi.fn(),
  updateProjectMemory: vi.fn(),
  archiveProjectMemory: vi.fn(),
  unarchiveProjectMemory: vi.fn(),
  deleteProjectMemory: vi.fn(),
}));

vi.mock('../../api/client', () => mocks);

const memory = (id: string, content: string, extra: Partial<Memory> = {}): Memory => ({
  id,
  user_id: 2,
  content,
  created_at: '2026-10-14T10:00:00Z',
  updated_at: null,
  archived: false,
  ...extra,
});

const rows = () => screen.getAllByRole('row').slice(1); // drop the header row
const rowTexts = () => rows().map((r) => r.textContent ?? '');

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
  mocks.fetchMemories.mockResolvedValue({
    memories: [
      memory('m1', 'Prefers bullet points'),
      memory('m2', 'Timezone: US/Pacific'),
      memory('m3', 'Ships the investor update on Fridays'),
    ],
  });
});

afterEach(cleanup);

describe('MemoriesSection', () => {
  it('lists memories in a table with a count and filters inline', async () => {
    render(<MemoriesSection />);
    await screen.findByText('Prefers bullet points');
    expect(mocks.fetchMemories).toHaveBeenCalledWith(false);
    expect(rows()).toHaveLength(3);
    expect(screen.getByText('3 memories')).not.toBeNull();

    fireEvent.change(screen.getByLabelText('Filter memories'), { target: { value: 'pacific' } });
    expect(rowTexts()).toEqual([expect.stringContaining('Timezone: US/Pacific')]);
    expect(screen.getByText('1 of 3')).not.toBeNull();
    expect(mocks.fetchMemories).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText('Filter memories'), { target: { value: 'nothing here' } });
    expect(screen.getByText('No memories match "nothing here". Archived memories are hidden.')).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Clear filter' }));
    expect(rows()).toHaveLength(3);
  });

  it('finds a memory by part of its id and shows the id on the match', async () => {
    mocks.fetchMemories.mockResolvedValue({
      memories: [
        memory('3f2a9c1e-7b44-4d0a-9e61-0c5d8a1b2f37', 'Prefers bullet points'),
        memory('b81d04e6-52c9-4f7e-a3d2-6e9f0a7c4b15', 'Timezone: US/Pacific'),
      ],
    });
    render(<MemoriesSection />);
    await screen.findByText('Prefers bullet points');
    expect(screen.queryByText('ID:')).toBeNull();

    // A substring from the middle of the id, in any case.
    fireEvent.change(screen.getByLabelText('Filter memories'), { target: { value: ' 52C9-4F7E ' } });
    expect(rows()).toHaveLength(1);
    expect(rowTexts()[0]).toContain('Timezone: US/Pacific');
    expect(screen.getByText('b81d04e6-52c9-4f7e-a3d2-6e9f0a7c4b15')).not.toBeNull();

    // Too short to be an id search: "3f2" is matched against the text only.
    fireEvent.change(screen.getByLabelText('Filter memories'), { target: { value: '3f2' } });
    expect(screen.getByText('No memories match "3f2". Archived memories are hidden.')).not.toBeNull();
  });

  it('shows the id of an expanded memory', async () => {
    render(<MemoriesSection />);
    fireEvent.click(await screen.findByText('Prefers bullet points'));
    expect(screen.getByText('m1')).not.toBeNull();
  });

  it('expands a row on click to show the full content', async () => {
    render(<MemoriesSection />);
    const text = await screen.findByText('Prefers bullet points');
    const toggle = text.closest('[role="button"]') as HTMLElement;
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(toggle.closest('tr')?.className).toContain('memory-row-expanded');
  });

  it('creates a memory from the toolbar and prepends it', async () => {
    mocks.createMemory.mockResolvedValue(memory('m4', 'Reads German'));
    render(<MemoriesSection />);
    await screen.findByText('Prefers bullet points');

    fireEvent.click(screen.getByRole('button', { name: '+ New memory' }));
    fireEvent.change(screen.getByLabelText('New memory'), { target: { value: 'Reads German' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(mocks.createMemory).toHaveBeenCalledWith('Reads German'));
    await waitFor(() => expect(rowTexts()[0]).toContain('Reads German'));
    expect(screen.queryByLabelText('New memory')).toBeNull();
    expect(screen.getByText('4 memories')).not.toBeNull();
  });

  it('edits in the row, saving with Ctrl+Enter and cancelling with Escape', async () => {
    mocks.updateMemory.mockImplementation(async (id: string, content: string) =>
      memory(id, content, { updated_at: '2026-10-15T10:00:00Z' }));
    render(<MemoriesSection />);
    await screen.findByText('Prefers bullet points');

    const row = rows()[0];
    fireEvent.click(within(row).getByRole('button', { name: 'Edit' }));
    const editor = screen.getByLabelText('Edit memory') as HTMLTextAreaElement;
    expect(editor.value).toBe('Prefers bullet points');
    fireEvent.keyDown(editor, { key: 'Escape' });
    expect(screen.queryByLabelText('Edit memory')).toBeNull();

    fireEvent.click(within(rows()[0]).getByRole('button', { name: 'Edit' }));
    const again = screen.getByLabelText('Edit memory');
    fireEvent.change(again, { target: { value: 'Prefers short bullet points' } });
    fireEvent.keyDown(again, { key: 'Enter', ctrlKey: true });
    await waitFor(() => expect(mocks.updateMemory).toHaveBeenCalledWith('m1', 'Prefers short bullet points'));
    await screen.findByText('Prefers short bullet points');
    expect(screen.queryByLabelText('Edit memory')).toBeNull();
  });

  it('archives out of the active view and shows archived rows on request', async () => {
    mocks.archiveMemory.mockResolvedValue(memory('m2', 'Timezone: US/Pacific', { archived: true }));
    render(<MemoriesSection />);
    await screen.findByText('Timezone: US/Pacific');

    fireEvent.click(within(rows()[1]).getByRole('button', { name: 'Archive' }));
    await waitFor(() => expect(rows()).toHaveLength(2));
    expect(screen.queryByText('Timezone: US/Pacific')).toBeNull();

    mocks.fetchMemories.mockResolvedValue({
      memories: [
        memory('m1', 'Prefers bullet points'),
        memory('m2', 'Timezone: US/Pacific', { archived: true }),
      ],
    });
    fireEvent.click(screen.getByLabelText('Show archived'));
    await waitFor(() => expect(mocks.fetchMemories).toHaveBeenLastCalledWith(true));
    const archivedRow = (await screen.findByText('Timezone: US/Pacific')).closest('tr') as HTMLElement;
    expect(archivedRow.className).toContain('memory-row-archived');
    expect(within(archivedRow).getByText('Archived')).not.toBeNull();
    expect(within(archivedRow).queryByRole('button', { name: 'Edit' })).toBeNull();
    expect(within(archivedRow).getByRole('button', { name: 'Unarchive' })).not.toBeNull();
  });
});

describe('MemoriesSection with a projectId', () => {
  it('lists, creates and deletes through the project endpoints', async () => {
    mocks.fetchProjectMemories.mockResolvedValue({
      memories: [memory('pm1', 'Figures are in EUR')],
    });
    mocks.createProjectMemory.mockResolvedValue(memory('pm2', 'Board meets monthly'));
    mocks.deleteProjectMemory.mockResolvedValue({ success: true });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<MemoriesSection projectId="proj-1" />);

    expect(screen.getByRole('heading', { name: 'Project Memories' })).not.toBeNull();
    await screen.findByText('Figures are in EUR');
    expect(mocks.fetchProjectMemories).toHaveBeenCalledWith('proj-1', false);
    expect(mocks.fetchMemories).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '+ New memory' }));
    const editor = screen.getByLabelText('New memory');
    expect((editor as HTMLTextAreaElement).placeholder).toContain('about this project');
    fireEvent.change(editor, { target: { value: 'Board meets monthly' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(mocks.createProjectMemory).toHaveBeenCalledWith('proj-1', 'Board meets monthly'));
    await screen.findByText('Board meets monthly');

    fireEvent.click(within(rows()[0]).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(mocks.deleteProjectMemory).toHaveBeenCalledWith('proj-1', 'pm2'));
    expect(mocks.createMemory).not.toHaveBeenCalled();
  });

  it('shows why an archived memory was archived when a reason is recorded', async () => {
    mocks.fetchMemories.mockResolvedValue({
      memories: [
        memory('m1', 'Reports to Dana', { archived: true, archive_reason: 'Dana left in October' }),
        memory('m2', 'Old timezone', { archived: true, archive_reason: null }),
        memory('m3', 'Prefers bullet points'),
      ],
    });
    render(<MemoriesSection />);
    await screen.findByText('Reports to Dana');
    const [withReason, withoutReason, active] = rows();
    expect(within(withReason).getByText('Dana left in October')).not.toBeNull();
    expect(within(withReason).getByText('Reason:')).not.toBeNull();
    expect(within(withoutReason).queryByText('Reason:')).toBeNull();
    expect(within(active).queryByText('Reason:')).toBeNull();
  });
});
