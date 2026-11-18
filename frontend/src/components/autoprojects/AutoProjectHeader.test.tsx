// The AutoProject page header's menu: Rename (inline input; Enter saves,
// Escape cancels, a 400 invalid_title keeps the input open), Archive
// (disabled while a run is active; a 409 autoproject_run_active explained),
// Unarchive on an archived AutoProject, Delete behind a confirm that leaves
// for the list, and the keyboard paths (Enter / ArrowDown / ArrowUp open with
// the focus on an item, arrows move, Escape closes back to the title, the
// single-key shortcuts). The jump button is covered for its label and target.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type { AutoProject } from '../../api/types';
import { AutoProjectHeader, type AutoProjectHeaderJump } from './AutoProjectHeader';

const mocks = vi.hoisted(() => ({
  updateAutoProject: vi.fn(),
  archiveAutoProject: vi.fn(),
  unarchiveAutoProject: vi.fn(),
  deleteAutoProject: vi.fn(),
}));

vi.mock('../../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/autoprojectsApi')>()),
  updateAutoProject: mocks.updateAutoProject,
  archiveAutoProject: mocks.archiveAutoProject,
  unarchiveAutoProject: mocks.unarchiveAutoProject,
  deleteAutoProject: mocks.deleteAutoProject,
}));

function autoproject(overrides: Partial<AutoProject> = {}): AutoProject {
  return {
    id: 'ap1',
    title: 'Market study',
    title_is_default: false,
    archived: false,
    brief: '',
    brief_version: 0,
    brief_updated_at: null,
    brief_source: null,
    brief_run_seq: null,
    model: null,
    thinking_effort: null,
    run_count: 2,
    active_run: null,
    event_seq: 3,
    unread_seq: 0,
    read_seq: 0,
    unread: false,
    created_at: '2026-11-01T00:00:00',
    updated_at: '2026-11-01T00:00:00',
    last_activity_at: '2026-11-01T00:00:00',
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

function renderHeader(ap: AutoProject = autoproject(), jump?: AutoProjectHeaderJump) {
  const onAutoProjectDetail = vi.fn();
  const utils = render(
    <MemoryRouter initialEntries={[`/autoprojects/${ap.id}`]}>
      <AutoProjectHeader autoproject={ap} onAutoProjectDetail={onAutoProjectDetail} jump={jump} />
      <LocationProbe />
    </MemoryRouter>,
  );
  return { ...utils, onAutoProjectDetail };
}

const titleButton = () => screen.getByRole('button', { name: /Market study/ });
const menu = () => screen.queryByRole('menu', { name: 'AutoProject actions' });
const item = (name: string) => screen.getByRole('menuitem', { name: new RegExp(`^${name}`) }) as HTMLButtonElement;

function openMenuByClick() {
  // A mouse click has detail 1 (detail 0 means a keyboard activation).
  fireEvent.click(titleButton(), { detail: 1 });
}

beforeEach(() => {
  mocks.updateAutoProject.mockReset();
  mocks.archiveAutoProject.mockReset();
  mocks.unarchiveAutoProject.mockReset();
  mocks.deleteAutoProject.mockReset();
});

afterEach(cleanup);

describe('AutoProjectHeader menu', () => {
  it('opens on a click and closes on an outside click', () => {
    renderHeader();
    expect(menu()).toBeNull();
    openMenuByClick();
    expect(menu()).not.toBeNull();
    expect(titleButton().getAttribute('aria-expanded')).toBe('true');
    expect(screen.getAllByRole('menuitem').map((b) => b.textContent)).toEqual(['RenameR', 'ArchiveA', 'DeleteD']);
    fireEvent.click(document.body);
    expect(menu()).toBeNull();
  });

  it('renames in place: Enter saves through PATCH and hands back the AutoProject', async () => {
    const renamed = autoproject({ title: 'Market study 2027' });
    mocks.updateAutoProject.mockResolvedValue(renamed);
    const { onAutoProjectDetail } = renderHeader();
    openMenuByClick();
    fireEvent.click(item('Rename'));

    const input = screen.getByRole('textbox', { name: 'Rename AutoProject' }) as HTMLInputElement;
    expect(input.value).toBe('Market study');
    expect(document.activeElement).toBe(input);
    fireEvent.change(input, { target: { value: '  Market study 2027 ' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() => expect(mocks.updateAutoProject).toHaveBeenCalledWith('ap1', { title: 'Market study 2027' }));
    await waitFor(() => expect(onAutoProjectDetail).toHaveBeenCalledWith(renamed));
    expect(screen.queryByRole('textbox', { name: 'Rename AutoProject' })).toBeNull();
  });

  it('cancels a rename on Escape and for an unchanged title', () => {
    renderHeader();
    openMenuByClick();
    fireEvent.click(item('Rename'));
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Rename AutoProject' }), { key: 'Escape' });
    expect(screen.queryByRole('textbox', { name: 'Rename AutoProject' })).toBeNull();
    expect(document.activeElement).toBe(titleButton());

    openMenuByClick();
    fireEvent.click(item('Rename'));
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Rename AutoProject' }), { key: 'Enter' });
    expect(mocks.updateAutoProject).not.toHaveBeenCalled();
  });

  it('keeps the input open with the message on a 400 invalid_title', async () => {
    mocks.updateAutoProject.mockRejectedValue(new ApiClientError('Title is too long.', 400, 'invalid_title'));
    renderHeader();
    openMenuByClick();
    fireEvent.click(item('Rename'));
    const input = screen.getByRole('textbox', { name: 'Rename AutoProject' });
    fireEvent.change(input, { target: { value: 'Bad' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect((await screen.findByRole('alert')).textContent).toBe('Title is too long.');
    expect(screen.getByRole('textbox', { name: 'Rename AutoProject' })).toBeTruthy();
    expect(input.getAttribute('aria-invalid')).toBe('true');
  });

  it('archives with a confirmation line', async () => {
    const archived = autoproject({ archived: true });
    mocks.archiveAutoProject.mockResolvedValue(archived);
    const { onAutoProjectDetail } = renderHeader();
    openMenuByClick();
    fireEvent.click(item('Archive'));
    await waitFor(() => expect(onAutoProjectDetail).toHaveBeenCalledWith(archived));
    expect(screen.getByRole('status').textContent).toBe(
      'AutoProject archived. It is hidden from the sidebar and takes no new prompts.',
    );
  });

  it('disables Archive while a run is active and explains a 409 autoproject_run_active', async () => {
    const { unmount } = renderHeader(autoproject({ active_run: { id: 'r1', seq: 3, status: 'running' } }));
    openMenuByClick();
    expect(item('Archive').disabled).toBe(true);
    unmount();

    mocks.archiveAutoProject.mockRejectedValue(new ApiClientError('busy', 409, 'autoproject_run_active'));
    renderHeader();
    openMenuByClick();
    fireEvent.click(item('Archive'));
    expect((await screen.findByRole('alert')).textContent).toContain(
      'An update is still in progress. Stop it or wait for it to finish before archiving.',
    );
  });

  it('offers Unarchive on an archived AutoProject, with the Archived chip', async () => {
    const restored = autoproject();
    mocks.unarchiveAutoProject.mockResolvedValue(restored);
    const { onAutoProjectDetail } = renderHeader(autoproject({ archived: true }));
    expect(screen.getByText('Archived')).toBeTruthy();
    openMenuByClick();
    expect(screen.queryByRole('menuitem', { name: /^Archive/ })).toBeNull();
    fireEvent.click(item('Unarchive'));
    await waitFor(() => expect(onAutoProjectDetail).toHaveBeenCalledWith(restored));
  });

  it('deletes after the confirm and leaves for the list the AutoProject was in', async () => {
    mocks.deleteAutoProject.mockResolvedValue({ success: true });
    renderHeader(autoproject({ archived: true, active_run: null }));
    openMenuByClick();
    fireEvent.click(item('Delete'));
    const dialog = screen.getByRole('dialog', { name: 'Delete AutoProject?' });
    expect(mocks.deleteAutoProject).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(mocks.deleteAutoProject).toHaveBeenCalledWith('ap1'));
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/autoprojects?archived=true'));
  });

  it('warns that a working update is stopped, and shows a failed delete in the dialog', async () => {
    mocks.deleteAutoProject.mockRejectedValue(new ApiClientError('Server error', 500));
    renderHeader(autoproject({ active_run: { id: 'r1', seq: 3, status: 'waiting' } }));
    openMenuByClick();
    fireEvent.click(item('Delete'));
    const dialog = screen.getByRole('dialog', { name: 'Delete AutoProject?' });
    expect(dialog.textContent).toContain('The update in progress will be stopped.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    expect((await within(dialog).findByRole('alert')).textContent).toContain('Server error');
    expect(screen.getByTestId('location').textContent).toBe('/autoprojects/ap1');
  });
});

describe('AutoProjectHeader keyboard', () => {
  it('opens with Enter on the first item, moves with the arrows and closes with Escape', () => {
    renderHeader();
    titleButton().focus();
    // A keyboard activation of a button is a click with detail 0.
    fireEvent.click(titleButton(), { detail: 0 });
    expect(document.activeElement).toBe(item('Rename'));

    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(item('Archive'));
    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(item('Delete'));
    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(item('Rename'));
    fireEvent.keyDown(document, { key: 'End' });
    expect(document.activeElement).toBe(item('Delete'));

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(menu()).toBeNull();
    expect(document.activeElement).toBe(titleButton());
  });

  it('opens with ArrowDown on the first item and ArrowUp on the last, skipping a disabled Archive', () => {
    renderHeader(autoproject({ active_run: { id: 'r1', seq: 1, status: 'running' } }));
    fireEvent.keyDown(titleButton(), { key: 'ArrowDown' });
    expect(document.activeElement).toBe(item('Rename'));
    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(item('Delete'));
    fireEvent.keyDown(document, { key: 'Escape' });

    fireEvent.keyDown(titleButton(), { key: 'ArrowUp' });
    expect(document.activeElement).toBe(item('Delete'));
  });

  it('acts on the single-key shortcuts while open', async () => {
    renderHeader();
    openMenuByClick();
    fireEvent.keyDown(document, { key: 'r' });
    expect(screen.getByRole('textbox', { name: 'Rename AutoProject' })).toBeTruthy();
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Rename AutoProject' }), { key: 'Escape' });

    openMenuByClick();
    fireEvent.keyDown(document, { key: 'd' });
    expect(screen.getByRole('dialog', { name: 'Delete AutoProject?' })).toBeTruthy();
  });
});

describe('AutoProjectHeader jump', () => {
  it.each([
    [{ target: 'latest', mark: 'working', up: false }, 'Jump to the newest update (in progress)', 'Latest'],
    [{ target: 'latest', mark: 'unread', up: true }, 'Jump to the newest update (not seen yet)', 'Latest'],
    [{ target: 'brief', mark: null, up: true }, 'Back to the brief', 'Brief'],
  ] as const)('labels the jump %#', (partial, label, text) => {
    const onJump = vi.fn();
    renderHeader(autoproject(), { ...partial, onJump });
    const button = screen.getByRole('button', { name: label });
    expect(button.textContent).toBe(text);
    fireEvent.click(button);
    expect(onJump).toHaveBeenCalledWith(partial.target);
  });
});
