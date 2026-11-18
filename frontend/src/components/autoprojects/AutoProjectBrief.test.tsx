// The Brief card and its in-place editor: the empty state, the byline,
// History, editing and saving with the base version, the 409 stale_brief
// merge box above the draft, a newer brief arriving while editing, Escape's
// discard confirm, the length cap and the phone clamp.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ApiClientError } from '../../api/request';
import type { AutoProject } from '../../api/types';
import { AutoProjectBrief } from './AutoProjectBrief';
import { makeAutoProject } from './autoprojectTestFixtures';

const mocks = vi.hoisted(() => ({
  putAutoProjectBrief: vi.fn(),
  isMobile: false,
}));

vi.mock('../../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/autoprojectsApi')>()),
  putAutoProjectBrief: mocks.putAutoProjectBrief,
}));

vi.mock('../../contexts/AppConfigContext', () => ({
  useAppConfig: () => ({ appName: 'Atlas' }),
}));

vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => mocks.isMobile }));

const BRIEF = makeAutoProject({
  brief: '# Goal\n\nShip the report.',
  brief_version: 3,
  brief_updated_at: '2026-11-10T10:00:00',
  brief_source: 'run:run-3',
  brief_run_seq: 3,
});

function renderBrief(autoproject: AutoProject, historyOpen = false) {
  const props = { onBrief: vi.fn(), onStale: vi.fn(), onOpenHistory: vi.fn(), onOpenFile: vi.fn() };
  const utils = render(<AutoProjectBrief autoproject={autoproject} historyOpen={historyOpen} {...props} />);
  return {
    ...utils,
    ...props,
    rerenderWith: (next: AutoProject) =>
      utils.rerender(<AutoProjectBrief autoproject={next} historyOpen={historyOpen} {...props} />),
  };
}

function textarea(): HTMLTextAreaElement {
  return screen.getByLabelText('Edit brief (markdown)') as HTMLTextAreaElement;
}

function saveButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
}

function staleError(brief: string, version: number) {
  return new ApiClientError('The brief changed', 409, 'stale_brief', undefined, {
    detail: {
      error: 'stale_brief',
      message: 'The brief changed',
      current: { brief, brief_version: version, brief_updated_at: '2026-11-10T11:00:00' },
    },
  });
}

beforeEach(() => {
  mocks.putAutoProjectBrief.mockReset();
  mocks.isMobile = false;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('AutoProjectBrief view', () => {
  it('says the first update writes an empty brief and offers to write it', () => {
    renderBrief(makeAutoProject());
    expect(screen.getByText('No brief yet. The first update writes it, or write one yourself.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Write the brief' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Brief history' })).toBeNull();
  });

  it('renders the brief with the update that wrote it', () => {
    const { container } = renderBrief(BRIEF);
    expect(screen.getByRole('heading', { name: 'Goal' })).toBeTruthy();
    const meta = container.querySelector('.autoproject-card-meta')!.textContent;
    expect(meta).toMatch(/^Updated .+ · update 3$/);
  });

  it.each([
    ['ui', null, /^Edited by you · /],
    ['restore:2', null, /^Restored from version 2 · /],
  ])('attributes a brief from %s', (source, runSeq, expected) => {
    const { container } = renderBrief({ ...BRIEF, brief_source: source, brief_run_seq: runSeq });
    expect(container.querySelector('.autoproject-card-meta')!.textContent).toMatch(expected);
  });

  it('opens History and marks it expanded while it shows', () => {
    const { onOpenHistory, unmount } = renderBrief(BRIEF);
    const history = screen.getByRole('button', { name: 'Brief history' });
    expect(history.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(history);
    expect(onOpenHistory).toHaveBeenCalledTimes(1);
    unmount();
    renderBrief(BRIEF, true);
    expect(screen.getByRole('button', { name: 'Brief history' }).getAttribute('aria-expanded')).toBe('true');
  });

  it('clamps a long brief on phones behind "Show full brief"', () => {
    mocks.isMobile = true;
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(2000);
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(600);
    renderBrief(BRIEF);
    const toggle = screen.getByRole('button', { name: 'Show full brief' });
    fireEvent.click(toggle);
    expect(screen.getByRole('button', { name: 'Show less' }).getAttribute('aria-expanded')).toBe('true');
  });

  it('never clamps the brief on a wide screen', () => {
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(2000);
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(600);
    renderBrief(BRIEF);
    expect(screen.queryByRole('button', { name: 'Show full brief' })).toBeNull();
  });
});

describe('AutoProjectBriefEditor', () => {
  it('edits in place and saves against the version the edit started from', async () => {
    mocks.putAutoProjectBrief.mockResolvedValue({ brief: 'New brief', brief_version: 4 });
    const { onBrief } = renderBrief(BRIEF);
    fireEvent.click(screen.getByRole('button', { name: 'Edit brief' }));

    expect(textarea().value).toBe(BRIEF.brief);
    expect(document.activeElement).toBe(textarea());
    expect(textarea().placeholder).toContain('What Atlas should know before each update');
    expect(saveButton().disabled).toBe(true);

    fireEvent.change(textarea(), { target: { value: 'New brief' } });
    fireEvent.click(saveButton());
    expect(mocks.putAutoProjectBrief).toHaveBeenCalledWith('ap1', 'New brief', 3);
    await waitFor(() => expect(onBrief).toHaveBeenCalledWith({ brief: 'New brief', brief_version: 4 }));
    expect(screen.queryByLabelText('Edit brief (markdown)')).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Edit brief' }));
  });

  it('shows the newer brief above the untouched draft on a 409 and saves against it next', async () => {
    mocks.putAutoProjectBrief.mockRejectedValueOnce(staleError('Their brief', 5));
    mocks.putAutoProjectBrief.mockResolvedValueOnce({ brief: 'My draft', brief_version: 6 });
    const { onStale, onBrief } = renderBrief(BRIEF);
    fireEvent.click(screen.getByRole('button', { name: 'Edit brief' }));
    fireEvent.change(textarea(), { target: { value: 'My draft' } });
    fireEvent.click(saveButton());

    const merge = await screen.findByRole('region', { name: 'Newer brief' });
    expect(within(merge).getByText('Their brief')).toBeTruthy();
    expect(within(merge).getByText(/now version 5/)).toBeTruthy();
    expect(textarea().value).toBe('My draft');
    // The merge box sits above the draft.
    expect(merge.compareDocumentPosition(textarea()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(onStale).toHaveBeenCalledTimes(1);

    fireEvent.click(saveButton());
    expect(mocks.putAutoProjectBrief).toHaveBeenLastCalledWith('ap1', 'My draft', 5);
    await waitFor(() => expect(onBrief).toHaveBeenCalledWith({ brief: 'My draft', brief_version: 6 }));
  });

  it('replaces the draft with the newer brief, with Undo, or keeps the draft', async () => {
    mocks.putAutoProjectBrief.mockRejectedValue(staleError('Their brief', 5));
    renderBrief(BRIEF);
    fireEvent.click(screen.getByRole('button', { name: 'Edit brief' }));
    fireEvent.change(textarea(), { target: { value: 'My draft' } });
    fireEvent.click(saveButton());
    await screen.findByRole('region', { name: 'Newer brief' });

    fireEvent.click(screen.getByRole('button', { name: 'Replace my draft with this' }));
    expect(textarea().value).toBe('Their brief');
    expect(screen.queryByRole('region', { name: 'Newer brief' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(textarea().value).toBe('My draft');

    fireEvent.click(saveButton());
    await screen.findByRole('region', { name: 'Newer brief' });
    fireEvent.click(screen.getByRole('button', { name: 'Keep my draft' }));
    expect(screen.queryByRole('region', { name: 'Newer brief' })).toBeNull();
    expect(textarea().value).toBe('My draft');
  });

  it('tells the editor when an update rewrote the brief meanwhile', () => {
    const { rerenderWith } = renderBrief(BRIEF);
    fireEvent.click(screen.getByRole('button', { name: 'Edit brief' }));
    fireEvent.change(textarea(), { target: { value: 'My draft' } });

    rerenderWith({ ...BRIEF, brief: 'Rewritten by update 4', brief_version: 4, brief_run_seq: 4, brief_source: 'run:run-4' });
    expect(screen.getByText(/This brief was updated by update 4 while\s+you were editing/)).toBeTruthy();
    expect(textarea().value).toBe('My draft');

    fireEvent.click(screen.getByRole('button', { name: 'Show the new version' }));
    const merge = screen.getByRole('region', { name: 'Newer brief' });
    expect(within(merge).getByText('Rewritten by update 4')).toBeTruthy();
  });

  it('cancels at once on Escape with nothing changed', () => {
    renderBrief(BRIEF);
    fireEvent.click(screen.getByRole('button', { name: 'Edit brief' }));
    fireEvent.keyDown(textarea(), { key: 'Escape' });
    expect(screen.queryByLabelText('Edit brief (markdown)')).toBeNull();
  });

  it('asks before Escape discards a changed draft', () => {
    renderBrief(BRIEF);
    fireEvent.click(screen.getByRole('button', { name: 'Edit brief' }));
    fireEvent.change(textarea(), { target: { value: 'My draft' } });

    fireEvent.keyDown(textarea(), { key: 'Escape' });
    expect(screen.getByText('Discard your changes?')).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Keep editing' }));

    // Escape on the confirm keeps editing with the draft intact.
    fireEvent.keyDown(screen.getByRole('button', { name: 'Keep editing' }), { key: 'Escape' });
    expect(screen.queryByText('Discard your changes?')).toBeNull();
    expect(textarea().value).toBe('My draft');
    expect(document.activeElement).toBe(textarea());

    fireEvent.keyDown(textarea(), { key: 'Escape' });
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }));
    expect(screen.queryByLabelText('Edit brief (markdown)')).toBeNull();
    expect(screen.getByRole('heading', { name: 'Goal' })).toBeTruthy();
    expect(mocks.putAutoProjectBrief).not.toHaveBeenCalled();
  });

  it('disables Save over the 12,000-character cap and says by how much', () => {
    renderBrief(BRIEF);
    fireEvent.click(screen.getByRole('button', { name: 'Edit brief' }));
    fireEvent.change(textarea(), { target: { value: 'x'.repeat(12_005) } });
    expect(screen.getByText('5 characters over the limit')).toBeTruthy();
    expect(saveButton().disabled).toBe(true);

    fireEvent.change(textarea(), { target: { value: 'x'.repeat(12_000) } });
    expect(screen.queryByText(/over the limit/)).toBeNull();
    expect(saveButton().disabled).toBe(false);
  });

  it('shows a refused save inline and stays open', async () => {
    mocks.putAutoProjectBrief.mockRejectedValue(
      new ApiClientError('Too long', 400, 'brief_too_long', undefined, {
        detail: { error: 'brief_too_long', message: 'Too long', cap: 12000, actual: 12010 },
      }),
    );
    renderBrief(BRIEF);
    fireEvent.click(screen.getByRole('button', { name: 'Edit brief' }));
    fireEvent.change(textarea(), { target: { value: 'My draft' } });
    fireEvent.click(saveButton());
    expect((await screen.findByRole('alert')).textContent).toBe(
      'The brief is 10 characters over the limit of 12,000.',
    );
    expect(textarea().value).toBe('My draft');
  });

  it('warns that an active update will merge the edit', () => {
    renderBrief({ ...BRIEF, active_run: { id: 'run-4', seq: 4, status: 'running' } });
    fireEvent.click(screen.getByRole('button', { name: 'Edit brief' }));
    expect(screen.getByText(/An update is in progress; if it also rewrites the brief, it will merge your edit/)).toBeTruthy();
  });
});
