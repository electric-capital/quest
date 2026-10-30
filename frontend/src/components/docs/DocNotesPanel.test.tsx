// DocNotesPanel: what the owner, a write share and a read share see and may
// do; the empty, loading and load-error states and docNotesPanelShown; the
// owner's instructions editor (save, cancel, clear, the version captured at
// open, the 409 merge view, other errors, the character counter at the
// cap); the memories' add / edit / delete with their inline errors and the
// 50-memory cap; focus after saves, failures and deletes; drafts dropped
// with a lost right; the "Suggestions for you" block (its rows, accept and
// dismiss through the view, the 409 re-base, inline errors, read shares);
// and the card vs details variants. The panel is presentational over
// `view`, so each test passes a fake DocNotesView.
// The clock is fixed (vi.setSystemTime), so the relative times are stable.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ApiClientError } from '../../api/request';
import type {
  AcceptDocNoteSuggestionResponse,
  DocMemory,
  DocNotes,
  DocNoteSuggestion,
} from '../../api/types';
import type { DocNotesView } from '../../hooks/useDocNotes';
import { DocNotesPanel, docNotesPanelShown, type DocNotesPanelProps } from './DocNotesPanel';

const STALE_MESSAGE =
  'The instructions changed since you started editing. Review the current text and save again.';
const CAP_MESSAGE = 'This doc already has the maximum of 50 memories. Delete or edit one instead.';
const EDITOR_LINE =
  "Models follow this doc's instructions and read its memories before they change it.";
const OWNER_ONLY_NOTE = "Only the doc's owner can change the instructions.";

const NOW = new Date('2026-10-29T12:00:00Z');

function minutesAgo(n: number): string {
  return new Date(NOW.getTime() - n * 60_000).toISOString();
}

const MEM_NEW: DocMemory = {
  id: 'm2',
  content: 'Prices are in USD.',
  source_label: 'Added by you',
  created_at: minutesAgo(10),
  updated_at: minutesAgo(10),
};

const MEM_OLD: DocMemory = {
  id: 'm1',
  content: 'The audience is\nthe board.',
  source_label: 'Suggested in a conversation',
  created_at: minutesAgo(300),
  updated_at: minutesAgo(180),
};

function makeNotes(overrides: Partial<DocNotes> = {}): DocNotes {
  return {
    doc_id: 'd1',
    instructions: 'Keep the tone formal.\nCite every source.',
    instructions_version: 3,
    instructions_updated_at: minutesAgo(5),
    instructions_updated_by: { id: 7, name: 'Ada Lovelace', email: 'Ada@Example.com' },
    memories: [MEM_NEW, MEM_OLD],
    memories_version: 2,
    can_edit: true,
    is_owner: true,
    ...overrides,
  };
}

const WRITE_SHARE: Partial<DocNotes> = {
  is_owner: false,
  can_edit: true,
  instructions_updated_by: null,
};
const READ_SHARE: Partial<DocNotes> = {
  is_owner: false,
  can_edit: false,
  instructions_updated_by: null,
};
const EMPTY: Partial<DocNotes> = {
  instructions: '',
  instructions_updated_at: null,
  instructions_updated_by: null,
  memories: [],
};

function makeView(overrides: Partial<DocNotesView> = {}): DocNotesView {
  return {
    enabled: true,
    notes: makeNotes(),
    suggestions: [],
    loading: false,
    error: null,
    notFound: false,
    suggestionsError: null,
    reload: vi.fn<DocNotesView['reload']>().mockResolvedValue(undefined),
    saveInstructions: vi.fn<DocNotesView['saveInstructions']>(),
    addMemory: vi.fn<DocNotesView['addMemory']>(),
    updateMemory: vi.fn<DocNotesView['updateMemory']>(),
    deleteMemory: vi.fn<DocNotesView['deleteMemory']>(),
    acceptSuggestion: vi.fn<DocNotesView['acceptSuggestion']>(),
    dismissSuggestion: vi.fn<DocNotesView['dismissSuggestion']>(),
    ...overrides,
  };
}

function viewWith(notes: Partial<DocNotes>, overrides: Partial<DocNotesView> = {}): DocNotesView {
  return makeView({ notes: makeNotes(notes), ...overrides });
}

function renderPanel(view: DocNotesView, props: Omit<DocNotesPanelProps, 'view'> = {}) {
  const result = render(<DocNotesPanel view={view} {...props} />);
  return {
    ...result,
    rerenderView: (next: DocNotesView) => result.rerender(<DocNotesPanel view={next} {...props} />),
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function staleError(
  current: { instructions: string; instructions_version: number } | null,
  message = STALE_MESSAGE,
) {
  const body: Record<string, unknown> = { error: 'stale_instructions', message };
  if (current) body.current = current;
  return new ApiClientError(message, 409, 'stale_instructions', undefined, body);
}

function duplicateError(memoryId: string) {
  const message = 'This doc already has a memory with exactly this text.';
  return new ApiClientError(message, 409, 'duplicate_memory', undefined, {
    detail: { error: 'duplicate_memory', message, memory_id: memoryId },
  });
}

const SUGGESTIONS_HINT = 'From your conversations. Nothing changes until you accept.';
const ACCEPT_STALE_MESSAGE =
  'The instructions changed since this was suggested. Review the current text and accept again.';

function makeSuggestion(overrides: Partial<DocNoteSuggestion> = {}): DocNoteSuggestion {
  return {
    id: 's1',
    user_id: 7,
    doc_id: 'd1',
    conversation_id: 'c1',
    note_kind: 'instructions',
    content: 'Keep the tone friendly.\nCite every source.',
    reasoning: 'The user asked for a lighter tone.',
    base_instructions_version: 3,
    replaces_memory_id: null,
    status: 'pending',
    memory_id: null,
    created_at: minutesAgo(30),
    resolved_at: null,
    doc_title: 'Quarterly plan',
    replaces_content: null,
    current_instructions: 'Keep the tone formal.\nCite every source.',
    current_instructions_version: 3,
    instructions_moved: false,
    ...overrides,
  };
}

const INSTRUCTIONS_SUGGESTION = makeSuggestion();
const MEMORY_SUGGESTION = makeSuggestion({
  id: 's2',
  note_kind: 'memory',
  content: 'The board meets weekly.',
  reasoning: null,
  base_instructions_version: null,
  current_instructions: null,
  current_instructions_version: null,
});

function acceptResponse(suggestion: DocNoteSuggestion): AcceptDocNoteSuggestionResponse {
  return {
    suggestion: { ...suggestion, status: 'accepted' },
    memory: null,
    notes: {
      doc_id: 'd1',
      instructions_version: 4,
      memories_version: 2,
      has_instructions: true,
      memory_count: 2,
    },
  };
}

const NEW_META = 'Added by you · 10m ago';
const OLD_META = 'Suggested in a conversation · 3h ago';

const instructionsBox = () => screen.getByRole('textbox', { name: 'Instructions' }) as HTMLTextAreaElement;
const addBox = () => screen.getByRole('textbox', { name: 'New memory' }) as HTMLTextAreaElement;
const editBox = () => screen.getByRole('textbox', { name: 'Edit memory' }) as HTMLTextAreaElement;
const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement;
// A memory's meta line ("<source> · <time>"; the time is in its own span).
const metaLine = (meta: string) => (_: string, el: Element | null) =>
  !!el?.classList.contains('doc-notes-memory-meta') && el.textContent === meta;
const memoryItem = (meta: string) => screen.getByText(metaLine(meta)).closest('li') as HTMLElement;
const editButtonOf = (meta: string) =>
  within(memoryItem(meta)).getByRole('button', { name: 'Edit memory' }) as HTMLButtonElement;
const deleteButtonOf = (meta: string) =>
  within(memoryItem(meta)).getByRole('button', { name: 'Delete memory' }) as HTMLButtonElement;
// The "Suggestions for you" block and its rows.
const suggestionsBlock = () =>
  screen.getByRole('heading', { name: 'Suggestions for you' }).closest(
    '.doc-notes-suggestions',
  ) as HTMLElement;
const suggestionRows = () =>
  [...suggestionsBlock().querySelectorAll<HTMLElement>('.memory-suggestion-row')];
const suggestionRow = (textLabel: 'Doc instructions text' | 'Doc memory text') =>
  screen.getByRole('textbox', { name: textLabel }).closest('.memory-suggestion-row') as HTMLElement;
const rowButton = (row: HTMLElement, name: 'Accept' | 'Dismiss') =>
  within(row).getByRole('button', { name }) as HTMLButtonElement;
const rowText = (row: HTMLElement) => within(row).getByRole('textbox') as HTMLTextAreaElement;
const rowDiffBody = (row: HTMLElement) => {
  const bodies = row.querySelectorAll('.memory-suggestion-diff-body');
  expect(bodies).toHaveLength(1);
  return bodies[0] as HTMLElement;
};

function openInstructionsEditor() {
  fireEvent.click(button('Edit instructions'));
  return instructionsBox();
}

describe('DocNotesPanel', () => {
  beforeEach(() => {
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  describe('rights', () => {
    it('owner: instructions with line breaks, "Edited by you", Edit, the Add row and memory buttons', () => {
      const { container } = renderPanel(makeView(), { viewerEmail: 'ada@example.com' });

      const text = container.querySelector('.doc-notes-instructions-text');
      expect(text?.textContent).toBe('Keep the tone formal.\nCite every source.');
      expect(screen.getByText('Edited by you, 5m ago')).toBeTruthy();
      expect(button('Edit instructions').textContent).toBe('Edit');
      expect(screen.queryByText(OWNER_ONLY_NOTE)).toBeNull();

      expect(screen.getByRole('textbox', { name: 'New memory' })).toBeTruthy();
      expect(button('Add').disabled).toBe(true);
      expect(screen.getAllByRole('button', { name: 'Edit memory' })).toHaveLength(2);
      expect(screen.getAllByRole('button', { name: 'Delete memory' })).toHaveLength(2);

      // Newest first as given, each with its source and age.
      const items = screen.getAllByRole('listitem');
      expect(items.map((li) => li.querySelector('.doc-notes-memory-content')?.textContent)).toEqual([
        'Prices are in USD.',
        'The audience is\nthe board.',
      ]);
      expect(within(items[0]).getByText(metaLine(NEW_META))).toBeTruthy();
      expect(within(items[1]).getByText(metaLine(OLD_META))).toBeTruthy();
      // The time never wraps inside itself.
      expect(items[0].querySelector('.doc-notes-memory-time')?.textContent).toBe('· 10m ago');
      expect(container.querySelector('.doc-notes-count')?.textContent).toBe('2');

      // The per-memory buttons are described by the memory they act on.
      const content = items[0].querySelector('.doc-notes-memory-content') as HTMLElement;
      for (const name of ['Edit memory', 'Delete memory']) {
        const btn = within(items[0]).getByRole('button', { name });
        expect(btn.getAttribute('aria-describedby')).toBe(content.id);
      }
      // Editors have focusable controls; the scroller is not a tab stop.
      expect(container.querySelector('.doc-notes-scroll')?.hasAttribute('tabindex')).toBe(false);
    });

    it('owner: names the last editor when the email differs or is unknown', () => {
      renderPanel(makeView(), { viewerEmail: 'grace@example.com' });
      expect(screen.getByText('Edited by Ada Lovelace, 5m ago')).toBeTruthy();
      cleanup();

      renderPanel(makeView());
      expect(screen.getByText('Edited by Ada Lovelace, 5m ago')).toBeTruthy();
      cleanup();

      renderPanel(
        viewWith({ instructions_updated_by: { id: 7, name: null, email: 'ada@example.com' } }),
        { viewerEmail: 'grace@example.com' },
      );
      expect(screen.getByText('Edited by ada@example.com, 5m ago')).toBeTruthy();
      cleanup();

      // Either field missing: no line.
      const { container } = renderPanel(viewWith({ instructions_updated_at: null }), {
        viewerEmail: 'ada@example.com',
      });
      expect(container.querySelector('.doc-notes-meta')).toBeNull();
    });

    it('write share: instructions read-only with the owner-only note, memories editable', () => {
      // The editor's identity is the owner's to see, even if a payload carried it.
      const { container } = renderPanel(
        viewWith({
          ...WRITE_SHARE,
          instructions_updated_by: { id: 7, name: 'Ada Lovelace', email: 'ada@example.com' },
        }),
        { viewerEmail: 'ada@example.com' },
      );

      expect(container.querySelector('.doc-notes-instructions-text')?.textContent).toBe(
        'Keep the tone formal.\nCite every source.',
      );
      expect(screen.getByText(OWNER_ONLY_NOTE)).toBeTruthy();
      expect(screen.queryByRole('button', { name: 'Edit instructions' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Add instructions' })).toBeNull();
      expect(screen.queryByText(/^Edited by/)).toBeNull();

      expect(screen.getByRole('textbox', { name: 'New memory' })).toBeTruthy();
      expect(screen.getAllByRole('button', { name: 'Edit memory' })).toHaveLength(2);
      expect(screen.getAllByRole('button', { name: 'Delete memory' })).toHaveLength(2);
    });

    it('write share: a doc with memories but no instructions says so', () => {
      renderPanel(viewWith({ ...WRITE_SHARE, instructions: '', instructions_updated_at: null }));
      expect(screen.getByRole('heading', { name: 'Instructions' })).toBeTruthy();
      expect(screen.getByText('No instructions.')).toBeTruthy();
      expect(screen.getByText(OWNER_ONLY_NOTE)).toBeTruthy();
    });

    it('read share: everything read-only, no buttons and no owner-only note', () => {
      const { container } = renderPanel(viewWith(READ_SHARE));

      expect(container.querySelector('.doc-notes-instructions-text')?.textContent).toBe(
        'Keep the tone formal.\nCite every source.',
      );
      expect(screen.getByText('Prices are in USD.')).toBeTruthy();
      expect(screen.queryByText(OWNER_ONLY_NOTE)).toBeNull();
      expect(screen.queryByRole('textbox')).toBeNull();
      expect(screen.queryAllByRole('button')).toHaveLength(0);
      // Nothing focusable inside: the scroller is a tab stop.
      expect(container.querySelector('.doc-notes-scroll')?.getAttribute('tabindex')).toBe('0');
    });

    it('read share: an empty memory list says so', () => {
      renderPanel(viewWith({ ...READ_SHARE, memories: [] }));
      expect(screen.getByText('No memories.')).toBeTruthy();
    });

    it('closes the instructions editor when the owner right drops, for good', () => {
      const view = makeView();
      const { rerenderView } = renderPanel(view);
      fireEvent.change(openInstructionsEditor(), { target: { value: 'Old draft' } });
      rerenderView(viewWith(WRITE_SHARE));
      expect(screen.queryByRole('textbox', { name: 'Instructions' })).toBeNull();
      expect(screen.getByText(OWNER_ONLY_NOTE)).toBeTruthy();

      // Re-granted: no old draft comes back.
      rerenderView(view);
      expect(screen.queryByRole('textbox', { name: 'Instructions' })).toBeNull();
      expect(openInstructionsEditor().value).toBe('Keep the tone formal.\nCite every source.');
    });

    it('drops the memory editor, the delete dialog and the Add draft when can_edit drops, for good', () => {
      const view = makeView();
      const { rerenderView } = renderPanel(view);
      fireEvent.change(addBox(), { target: { value: 'Unsent' } });
      fireEvent.click(editButtonOf(NEW_META));
      fireEvent.change(editBox(), { target: { value: 'Half-edited' } });
      fireEvent.click(deleteButtonOf(OLD_META));
      expect(screen.getByRole('dialog')).toBeTruthy();

      rerenderView(viewWith(READ_SHARE));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.queryByRole('textbox')).toBeNull();
      expect(screen.getByText('Prices are in USD.')).toBeTruthy();

      rerenderView(view);
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.queryByRole('textbox', { name: 'Edit memory' })).toBeNull();
      expect(addBox().value).toBe('');
    });
  });

  describe('empty state', () => {
    it('owner: the one-line explanation, Add instructions and the Add row', () => {
      renderPanel(viewWith(EMPTY));
      expect(screen.getByText(EDITOR_LINE)).toBeTruthy();
      expect(button('Add instructions')).toBeTruthy();
      expect(screen.queryByRole('button', { name: 'Edit instructions' })).toBeNull();
      expect(screen.getByRole('textbox', { name: 'New memory' })).toBeTruthy();
      expect(screen.queryByText('No memories.')).toBeNull();
    });

    it('write share: the explanation and the owner-only note, no Add instructions', () => {
      renderPanel(viewWith({ ...EMPTY, ...WRITE_SHARE }));
      expect(screen.getByText(EDITOR_LINE)).toBeTruthy();
      expect(screen.getByText(OWNER_ONLY_NOTE)).toBeTruthy();
      expect(screen.queryByRole('button', { name: 'Add instructions' })).toBeNull();
      expect(screen.getByRole('textbox', { name: 'New memory' })).toBeTruthy();
    });

    it('read share: only the no-notes line', () => {
      renderPanel(viewWith({ ...EMPTY, ...READ_SHARE }));
      expect(screen.getByText('This doc has no notes.')).toBeTruthy();
      expect(screen.queryByText(EDITOR_LINE)).toBeNull();
      expect(screen.queryByRole('heading', { name: 'Instructions' })).toBeNull();
      expect(screen.queryByRole('heading', { name: 'Memories' })).toBeNull();
      expect(screen.queryByText('No memories.')).toBeNull();
    });

    it('read share with memories but no instructions: no Instructions section', () => {
      renderPanel(viewWith({ ...READ_SHARE, instructions: '', instructions_updated_at: null }));
      expect(screen.queryByRole('heading', { name: 'Instructions' })).toBeNull();
      expect(screen.getByRole('heading', { name: 'Memories' })).toBeTruthy();
      expect(screen.queryByText('This doc has no notes.')).toBeNull();
    });
  });

  describe('loading, errors and docNotesPanelShown', () => {
    it('shows a loading line', () => {
      const view = makeView({ notes: null, loading: true });
      expect(docNotesPanelShown(view)).toBe(true);
      renderPanel(view);
      expect(screen.getByText('Loading notes...')).toBeTruthy();
    });

    it('shows a load error with Retry calling reload, disabled until it settles', async () => {
      const pending = deferred<void>();
      const view = makeView({ notes: null, error: new ApiClientError('Server exploded', 500) });
      vi.mocked(view.reload).mockReturnValue(pending.promise);
      expect(docNotesPanelShown(view)).toBe(true);
      renderPanel(view);
      expect(screen.getByText('Server exploded')).toBeTruthy();

      fireEvent.click(button('Retry'));
      expect(view.reload).toHaveBeenCalledTimes(1);
      expect(button('Retrying...').disabled).toBe(true);

      await act(async () => {
        pending.resolve();
      });
      expect(button('Retry').disabled).toBe(false);
    });

    it('falls back to a generic load error message', () => {
      renderPanel(makeView({ notes: null, error: new ApiClientError('') }));
      expect(screen.getByText('Failed to load notes.')).toBeTruthy();
    });

    it.each([
      ['the hook is disabled', { enabled: false, notes: null }],
      ['the doc is not found', { notes: null, notFound: true }],
      [
        'the notes are refused with doc_notes_disabled',
        {
          notes: null,
          error: new ApiClientError('Doc notes are not available', 403, 'doc_notes_disabled'),
        },
      ],
    ] as [string, Partial<DocNotesView>][])('renders nothing when %s', (_label, overrides) => {
      const view = makeView(overrides);
      expect(docNotesPanelShown(view)).toBe(false);
      const { container } = renderPanel(view);
      expect(container.innerHTML).toBe('');
      cleanup();
      const details = renderPanel(view, { variant: 'details' });
      expect(details.container.innerHTML).toBe('');
    });

    it('is shown with notes', () => {
      expect(docNotesPanelShown(makeView())).toBe(true);
    });
  });

  describe('instructions editor', () => {
    it('Edit opens a focused textarea with the current text; Save sends it with the version', async () => {
      const view = makeView();
      const pending = deferred<DocNotes>();
      vi.mocked(view.saveInstructions).mockReturnValue(pending.promise);
      renderPanel(view);

      const box = openInstructionsEditor();
      expect(box.value).toBe('Keep the tone formal.\nCite every source.');
      expect(document.activeElement).toBe(box);
      expect(screen.queryByRole('button', { name: 'Edit instructions' })).toBeNull();

      fireEvent.change(box, { target: { value: 'Be brief.\nNo emojis.' } });
      fireEvent.click(button('Save'));
      expect(view.saveInstructions).toHaveBeenCalledWith('Be brief.\nNo emojis.', 3);

      // Pending: "Saving..." and nothing else to press or type.
      expect(button('Saving...').disabled).toBe(true);
      expect(button('Cancel').disabled).toBe(true);
      expect(instructionsBox().readOnly).toBe(true);

      await act(async () => {
        pending.resolve(makeNotes({ instructions: 'Be brief.\nNo emojis.', instructions_version: 4 }));
      });
      expect(screen.queryByRole('textbox', { name: 'Instructions' })).toBeNull();
      await waitFor(() => expect(document.activeElement).toBe(button('Edit instructions')));
    });

    it('Cancel discards the draft and returns focus to Edit', () => {
      const view = makeView();
      renderPanel(view);
      fireEvent.change(openInstructionsEditor(), { target: { value: 'Draft' } });
      fireEvent.click(button('Cancel'));

      expect(screen.queryByRole('textbox', { name: 'Instructions' })).toBeNull();
      expect(document.activeElement).toBe(button('Edit instructions'));
      expect(view.saveInstructions).not.toHaveBeenCalled();
      expect(openInstructionsEditor().value).toBe('Keep the tone formal.\nCite every source.');
    });

    it('clearing the text sends "" to remove the instructions', async () => {
      const view = makeView();
      vi.mocked(view.saveInstructions).mockResolvedValue(makeNotes(EMPTY));
      renderPanel(view);
      fireEvent.change(openInstructionsEditor(), { target: { value: '  \n ' } });
      fireEvent.click(button('Save'));
      expect(view.saveInstructions).toHaveBeenCalledWith('', 3);
      await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Instructions' })).toBeNull());
    });

    it('sends the version the editor opened with, not a newer one that arrived while editing', () => {
      const view = makeView();
      vi.mocked(view.saveInstructions).mockReturnValue(new Promise(() => {}));
      const { rerenderView } = renderPanel(view);
      fireEvent.change(openInstructionsEditor(), { target: { value: 'My draft' } });

      rerenderView({
        ...view,
        notes: makeNotes({ instructions: 'Changed elsewhere', instructions_version: 5 }),
      });
      expect(instructionsBox().value).toBe('My draft');
      fireEvent.click(button('Save'));
      expect(view.saveInstructions).toHaveBeenCalledWith('My draft', 3);
    });

    it('Add instructions opens an empty, focused editor on an empty doc', () => {
      const view = viewWith(EMPTY);
      vi.mocked(view.saveInstructions).mockReturnValue(new Promise(() => {}));
      renderPanel(view);
      fireEvent.click(button('Add instructions'));
      const box = instructionsBox();
      expect(box.value).toBe('');
      expect(document.activeElement).toBe(box);
      fireEvent.change(box, { target: { value: 'First rule' } });
      fireEvent.click(button('Save'));
      expect(view.saveInstructions).toHaveBeenCalledWith('First rule', 3);
    });

    it('a 409 keeps the draft, shows the current text above it and saves against its version', async () => {
      const view = makeView();
      const save = vi.mocked(view.saveInstructions);
      save.mockRejectedValueOnce(staleError({ instructions: 'Server text', instructions_version: 7 }));
      renderPanel(view);
      fireEvent.change(openInstructionsEditor(), { target: { value: 'My draft' } });
      fireEvent.click(button('Save'));

      const firstNotice = await screen.findByRole('alert');
      expect(firstNotice.textContent).toBe(STALE_MESSAGE);
      const current = screen.getByRole('group', { name: 'Current instructions' });
      expect(within(current).getByText('Server text')).toBeTruthy();
      // Shown whole, not a scroll box of its own, so not a tab stop.
      expect(within(current).getByText('Server text').hasAttribute('tabindex')).toBe(false);
      const box = instructionsBox();
      expect(document.activeElement).toBe(box);
      expect(box.value).toBe('My draft');
      expect(box.readOnly).toBe(false);
      expect(current.compareDocumentPosition(box) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

      // A second 409 replaces the shown text and version.
      save.mockRejectedValueOnce(staleError({ instructions: 'Newer text', instructions_version: 8 }));
      fireEvent.click(button('Save'));
      expect(save).toHaveBeenLastCalledWith('My draft', 7);
      await waitFor(() =>
        expect(
          within(screen.getByRole('group', { name: 'Current instructions' })).getByText('Newer text'),
        ).toBeTruthy(),
      );
      expect(screen.queryByText('Server text')).toBeNull();
      // A new notice element, so the second 409 is announced again.
      expect(screen.getByRole('alert')).not.toBe(firstNotice);
      expect(firstNotice.isConnected).toBe(false);

      save.mockResolvedValueOnce(makeNotes({ instructions: 'My draft', instructions_version: 9 }));
      fireEvent.click(button('Save'));
      expect(save).toHaveBeenLastCalledWith('My draft', 8);
      await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Instructions' })).toBeNull());
    });

    it('a 409 for removed instructions says so; Cancel leaves the merge view', async () => {
      const view = makeView();
      vi.mocked(view.saveInstructions).mockRejectedValueOnce(
        staleError({ instructions: '', instructions_version: 6 }, ''),
      );
      renderPanel(view);
      fireEvent.change(openInstructionsEditor(), { target: { value: 'My draft' } });
      fireEvent.click(button('Save'));

      // The default notice when the server sent no message.
      expect((await screen.findByRole('alert')).textContent).toBe(STALE_MESSAGE);
      expect(
        within(screen.getByRole('group', { name: 'Current instructions' })).getByText(
          'The instructions were removed.',
        ),
      ).toBeTruthy();

      fireEvent.click(button('Cancel'));
      expect(screen.queryByRole('group', { name: 'Current instructions' })).toBeNull();
      openInstructionsEditor();
      expect(screen.queryByRole('group', { name: 'Current instructions' })).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('a 409 without a current payload falls back to the live notes', async () => {
      const view = makeView();
      const save = vi.mocked(view.saveInstructions);
      save.mockRejectedValueOnce(staleError(null));
      const { rerenderView } = renderPanel(view);
      fireEvent.change(openInstructionsEditor(), { target: { value: 'My draft' } });
      fireEvent.click(button('Save'));
      await screen.findByRole('alert');

      // The hook re-fetched the notes meanwhile.
      rerenderView({ ...view, notes: makeNotes({ instructions: 'Live text', instructions_version: 11 }) });
      expect(
        within(screen.getByRole('group', { name: 'Current instructions' })).getByText('Live text'),
      ).toBeTruthy();
      save.mockReturnValueOnce(new Promise(() => {}));
      fireEvent.click(button('Save'));
      expect(save).toHaveBeenLastCalledWith('My draft', 11);
    });

    it('shows other errors inline, keeps the editor open and refocuses the draft', async () => {
      const view = makeView();
      vi.mocked(view.saveInstructions).mockRejectedValueOnce(
        new ApiClientError('Instructions cannot contain NUL characters.', 400, 'invalid_instructions'),
      );
      renderPanel(view);
      fireEvent.change(openInstructionsEditor(), { target: { value: 'Bad draft' } });
      // The pressed Save is disabled while saving.
      button('Save').focus();
      fireEvent.click(button('Save'));

      expect((await screen.findByRole('alert')).textContent).toBe(
        'Instructions cannot contain NUL characters.',
      );
      expect(instructionsBox().value).toBe('Bad draft');
      expect(document.activeElement).toBe(instructionsBox());
      expect(screen.queryByRole('group', { name: 'Current instructions' })).toBeNull();
      expect(button('Save').disabled).toBe(false);
    });

    describe('character counter', () => {
      it('counts trimmed code points and disables Save over 4,000', () => {
        renderPanel(makeView());
        const box = openInstructionsEditor();
        const counter = screen.getByText('40 / 4,000');
        expect(box.getAttribute('aria-describedby')?.split(' ')).toContain(counter.id);

        fireEvent.change(box, { target: { value: 'a'.repeat(4000) } });
        expect(screen.getByText('4,000 / 4,000')).toBeTruthy();
        expect(button('Save').disabled).toBe(false);
        expect(screen.queryByText('Use at most 4,000 characters.')).toBeNull();
        expect(box.hasAttribute('aria-invalid')).toBe(false);

        fireEvent.change(box, { target: { value: 'a'.repeat(4001) } });
        expect(screen.getByText('4,001 / 4,000')).toBeTruthy();
        expect(button('Save').disabled).toBe(true);
        const over = screen.getByText('Use at most 4,000 characters.');
        expect(box.getAttribute('aria-describedby')?.split(' ')).toContain(over.id);
        expect(box.getAttribute('aria-invalid')).toBe('true');

        // Surrounding whitespace is not counted.
        fireEvent.change(box, { target: { value: `  \n${'a'.repeat(4000)}\n  ` } });
        expect(screen.getByText('4,000 / 4,000')).toBeTruthy();
        expect(button('Save').disabled).toBe(false);

        // An astral emoji is one character (two UTF-16 units).
        fireEvent.change(box, { target: { value: '😀'.repeat(4000) } });
        expect(screen.getByText('4,000 / 4,000')).toBeTruthy();
        expect(button('Save').disabled).toBe(false);
        fireEvent.change(box, { target: { value: `${'😀'.repeat(4000)}b` } });
        expect(screen.getByText('4,001 / 4,000')).toBeTruthy();
        expect(button('Save').disabled).toBe(true);
      });
    });
  });

  describe('memories: add', () => {
    it('Add is disabled for empty or blank text and the counter appears once there is text', () => {
      renderPanel(makeView());
      expect(button('Add').disabled).toBe(true);
      expect(screen.queryByText(/\/ 1,000$/)).toBeNull();
      fireEvent.change(addBox(), { target: { value: '   ' } });
      expect(button('Add').disabled).toBe(true);
      fireEvent.change(addBox(), { target: { value: 'abc' } });
      expect(button('Add').disabled).toBe(false);
      expect(screen.getByText('3 / 1,000')).toBeTruthy();
    });

    it('adds the trimmed text and clears the field', async () => {
      const view = makeView();
      const pending = deferred<DocMemory>();
      vi.mocked(view.addMemory).mockReturnValue(pending.promise);
      renderPanel(view);

      fireEvent.change(addBox(), { target: { value: '  Use metric units.  ' } });
      fireEvent.click(button('Add'));
      expect(view.addMemory).toHaveBeenCalledWith('Use metric units.');
      expect(button('Adding...').disabled).toBe(true);
      expect(addBox().readOnly).toBe(true);

      await act(async () => {
        pending.resolve({ ...MEM_NEW, id: 'm3', content: 'Use metric units.' });
      });
      expect(addBox().value).toBe('');
      expect(addBox().readOnly).toBe(false);
      expect(document.activeElement).toBe(addBox());
    });

    it('a duplicate shows inline, keeps the draft and refocuses it, and highlights the existing memory', async () => {
      const scrollIntoView = vi.fn();
      Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
        value: scrollIntoView,
        configurable: true,
        writable: true,
      });
      try {
        const view = makeView();
        vi.mocked(view.addMemory).mockRejectedValueOnce(duplicateError('m1'));
        renderPanel(view);
        fireEvent.change(addBox(), { target: { value: 'The audience is\nthe board.' } });
        // The pressed Add is disabled while adding.
        button('Add').focus();
        fireEvent.click(button('Add'));

        expect((await screen.findByRole('alert')).textContent).toBe(
          'This doc already has that memory.',
        );
        expect(addBox().value).toBe('The audience is\nthe board.');
        expect(document.activeElement).toBe(addBox());
        expect(memoryItem(OLD_META).className).toContain('doc-notes-memory--highlight');
        expect(memoryItem(NEW_META).className).not.toContain('doc-notes-memory--highlight');
        expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' });
        expect(scrollIntoView.mock.contexts[0]).toBe(memoryItem(OLD_META));
      } finally {
        delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView;
      }
    });

    it('the highlight clears after its timeout, restarted by a repeat duplicate', async () => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
      const view = makeView();
      vi.mocked(view.addMemory).mockRejectedValue(duplicateError('m1'));
      renderPanel(view);
      const highlighted = () =>
        memoryItem(OLD_META).classList.contains('doc-notes-memory--highlight');
      const addDuplicate = async () => {
        await act(async () => {
          fireEvent.click(button('Add'));
        });
      };

      fireEvent.change(addBox(), { target: { value: 'The audience is\nthe board.' } });
      await addDuplicate();
      expect(highlighted()).toBe(true);
      act(() => {
        vi.advanceTimersByTime(1500);
      });
      expect(highlighted()).toBe(true);

      // The same duplicate again: the 2 s start over.
      await addDuplicate();
      act(() => {
        vi.advanceTimersByTime(1500);
      });
      expect(highlighted()).toBe(true);
      act(() => {
        vi.advanceTimersByTime(500);
      });
      expect(highlighted()).toBe(false);
    });

    it('the memory cap error shows the server message inline', async () => {
      const view = makeView();
      vi.mocked(view.addMemory).mockRejectedValueOnce(
        new ApiClientError(CAP_MESSAGE, 409, 'memories_cap_reached'),
      );
      renderPanel(view);
      fireEvent.change(addBox(), { target: { value: 'One more' } });
      fireEvent.click(button('Add'));
      expect((await screen.findByRole('alert')).textContent).toBe(CAP_MESSAGE);
      expect(addBox().value).toBe('One more');
    });

    it('at 50 memories the Add row is replaced by the max line', () => {
      const memories = Array.from({ length: 50 }, (_, i) => ({
        ...MEM_NEW,
        id: `m${i}`,
        content: `Fact ${i}`,
      }));
      renderPanel(viewWith({ memories }));
      expect(screen.queryByRole('textbox', { name: 'New memory' })).toBeNull();
      expect(
        screen.getByText('This doc has the maximum of 50 memories. Delete one to add another.'),
      ).toBeTruthy();
      expect(screen.getAllByRole('button', { name: 'Delete memory' })).toHaveLength(50);
    });

    it('over 1,000 characters disables Add', () => {
      renderPanel(makeView());
      fireEvent.change(addBox(), { target: { value: 'x'.repeat(1000) } });
      expect(button('Add').disabled).toBe(false);
      expect(screen.getByText('1,000 / 1,000')).toBeTruthy();
      fireEvent.change(addBox(), { target: { value: 'x'.repeat(1001) } });
      expect(button('Add').disabled).toBe(true);
      expect(screen.getByText('Use at most 1,000 characters.')).toBeTruthy();
    });
  });

  describe('memories: edit', () => {
    it('saves the edited text and returns focus to the memory\'s Edit button', async () => {
      const view = makeView();
      vi.mocked(view.updateMemory).mockResolvedValue({ ...MEM_NEW, content: 'Prices are in EUR.' });
      renderPanel(view);

      fireEvent.click(editButtonOf(NEW_META));
      const box = editBox();
      expect(box.value).toBe('Prices are in USD.');
      expect(document.activeElement).toBe(box);
      expect(screen.getByText('18 / 1,000')).toBeTruthy();

      fireEvent.change(box, { target: { value: 'Prices are in EUR. ' } });
      fireEvent.click(button('Save'));
      expect(view.updateMemory).toHaveBeenCalledWith('m2', 'Prices are in EUR.');
      await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Edit memory' })).toBeNull());
      await waitFor(() => expect(document.activeElement).toBe(editButtonOf(NEW_META)));
    });

    it('shows duplicate and not-found errors inline under the editor, refocusing the draft', async () => {
      const view = makeView();
      vi.mocked(view.updateMemory)
        .mockRejectedValueOnce(duplicateError('m1'))
        .mockRejectedValueOnce(new ApiClientError('Doc memory not found.', 404, 'memory_not_found'));
      renderPanel(view);
      fireEvent.click(editButtonOf(NEW_META));
      fireEvent.change(editBox(), { target: { value: 'The audience is\nthe board.' } });

      button('Save').focus();
      fireEvent.click(button('Save'));
      expect((await screen.findByRole('alert')).textContent).toBe('This doc already has that memory.');
      expect(editBox().value).toBe('The audience is\nthe board.');
      expect(document.activeElement).toBe(editBox());
      expect(memoryItem(OLD_META).className).toContain('doc-notes-memory--highlight');

      fireEvent.click(button('Save'));
      await waitFor(() =>
        expect(screen.getByRole('alert').textContent).toBe('This memory was deleted.'),
      );
      expect(editBox()).toBeTruthy();
    });

    it('Cancel restores the memory unchanged', () => {
      const view = makeView();
      renderPanel(view);
      fireEvent.click(editButtonOf(NEW_META));
      fireEvent.change(editBox(), { target: { value: 'Something else' } });
      fireEvent.click(button('Cancel'));

      expect(screen.queryByRole('textbox', { name: 'Edit memory' })).toBeNull();
      expect(screen.getByText('Prices are in USD.')).toBeTruthy();
      expect(view.updateMemory).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(editButtonOf(NEW_META));
    });

    it('keeps one editor open at a time and closes it when its memory disappears', () => {
      const view = makeView();
      const { rerenderView } = renderPanel(view);
      fireEvent.click(editButtonOf(NEW_META));
      fireEvent.click(editButtonOf(OLD_META));
      expect(screen.getAllByRole('textbox', { name: 'Edit memory' })).toHaveLength(1);
      expect(editBox().value).toBe('The audience is\nthe board.');

      rerenderView({ ...view, notes: makeNotes({ memories: [MEM_NEW] }) });
      expect(screen.queryByRole('textbox', { name: 'Edit memory' })).toBeNull();
    });

    it('ignores starting another memory edit while a save is in flight', async () => {
      const view = makeView();
      const pending = deferred<DocMemory>();
      vi.mocked(view.updateMemory).mockReturnValue(pending.promise);
      renderPanel(view);
      fireEvent.click(editButtonOf(NEW_META));
      fireEvent.change(editBox(), { target: { value: 'Prices are in EUR.' } });
      fireEvent.click(button('Save'));

      const other = editButtonOf(OLD_META);
      expect(other.disabled).toBe(true);
      fireEvent.click(other);
      expect(editBox().value).toBe('Prices are in EUR.');
      expect(button('Saving...')).toBeTruthy();

      // The failing save's error is still shown where it belongs.
      await act(async () => {
        pending.reject(new ApiClientError('Server exploded', 500));
      });
      expect(screen.getByRole('alert').textContent).toBe('Server exploded');
      expect(editBox().value).toBe('Prices are in EUR.');
      expect(editButtonOf(OLD_META).disabled).toBe(false);
    });

    it('disables Save for blank or over-cap text', () => {
      renderPanel(makeView());
      fireEvent.click(editButtonOf(NEW_META));
      fireEvent.change(editBox(), { target: { value: '  ' } });
      expect(button('Save').disabled).toBe(true);
      fireEvent.change(editBox(), { target: { value: 'y'.repeat(1001) } });
      expect(button('Save').disabled).toBe(true);
      expect(screen.getByText('Use at most 1,000 characters.')).toBeTruthy();
    });
  });

  describe('memories: delete', () => {
    const confirm = () =>
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));

    it('confirms with the memory quoted, deletes it and focuses the next memory', async () => {
      const view = makeView();
      const pending = deferred<void>();
      vi.mocked(view.deleteMemory).mockReturnValue(pending.promise);
      const { rerenderView } = renderPanel(view);

      fireEvent.click(deleteButtonOf(NEW_META));
      const dialog = screen.getByRole('dialog', { name: 'Delete this memory?' });
      expect(within(dialog).getByText('“Prices are in USD.”')).toBeTruthy();
      expect(view.deleteMemory).not.toHaveBeenCalled();

      confirm();
      expect(view.deleteMemory).toHaveBeenCalledWith('m2');
      // As the hook does: the list loses the memory, then the call resolves.
      rerenderView({ ...view, notes: makeNotes({ memories: [MEM_OLD] }) });
      await act(async () => {
        pending.resolve();
      });
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(document.activeElement).toBe(editButtonOf(OLD_META));
    });

    it('focuses the previous memory after deleting the last one', async () => {
      const view = makeView();
      vi.mocked(view.deleteMemory).mockResolvedValue(undefined);
      renderPanel(view);
      fireEvent.click(deleteButtonOf(OLD_META));
      confirm();
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(document.activeElement).toBe(editButtonOf(NEW_META));
    });

    it('focuses the Add field after deleting the only memory', async () => {
      const view = viewWith({ memories: [MEM_NEW] });
      vi.mocked(view.deleteMemory).mockResolvedValue(undefined);
      renderPanel(view);
      fireEvent.click(deleteButtonOf(NEW_META));
      confirm();
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(document.activeElement).toBe(addBox());
    });

    it('truncates a long memory in the quote', () => {
      renderPanel(viewWith({ memories: [{ ...MEM_NEW, content: 'x'.repeat(300) }] }));
      fireEvent.click(button('Delete memory'));
      const quote = screen.getByRole('dialog').querySelector('.doc-notes-dialog-quote');
      expect(quote?.textContent).toBe(`“${'x'.repeat(200)}…”`);
    });

    it('shows a failure inside the dialog', async () => {
      const view = makeView();
      vi.mocked(view.deleteMemory).mockRejectedValue(new ApiClientError('Server exploded', 500));
      renderPanel(view);
      fireEvent.click(deleteButtonOf(NEW_META));
      confirm();

      const alert = await within(screen.getByRole('dialog')).findByRole('alert');
      expect(alert.textContent).toBe('Server exploded');
    });

    it('closes the dialog when the memory is already gone, focusing the next memory', async () => {
      const view = makeView();
      vi.mocked(view.deleteMemory).mockRejectedValue(
        new ApiClientError('Doc memory not found.', 404, 'memory_not_found'),
      );
      renderPanel(view);
      fireEvent.click(deleteButtonOf(NEW_META));
      confirm();
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(document.activeElement).toBe(editButtonOf(OLD_META));
    });
  });

  describe('suggestions for you', () => {
    const withSuggestions = (
      suggestions: DocNoteSuggestion[],
      notes: Partial<DocNotes> = {},
    ): DocNotesView => viewWith(notes, { suggestions });

    it('is not shown without suggestions', () => {
      const { container } = renderPanel(makeView());
      expect(screen.queryByRole('heading', { name: 'Suggestions for you' })).toBeNull();
      expect(container.querySelector('.doc-notes-suggestions')).toBeNull();
      expect(container.querySelector('.memory-suggestion-row')).toBeNull();
    });

    it('shows the count, the hint and one row per suggestion, above the empty line and Instructions', () => {
      renderPanel(withSuggestions([INSTRUCTIONS_SUGGESTION, MEMORY_SUGGESTION], EMPTY));
      const block = suggestionsBlock();
      expect(block.querySelector('.doc-notes-count')?.textContent).toBe('2');
      expect(within(block).getByText(SUGGESTIONS_HINT)).toBeTruthy();
      expect(suggestionRows()).toHaveLength(2);

      // The instructions row: its pill, the proposal and the diff from the
      // doc's current instructions.
      const instructions = suggestionRow('Doc instructions text');
      expect(instructions.classList.contains('memory-suggestion-row--doc')).toBe(true);
      expect(instructions.querySelector('.memory-suggestion-pill')?.textContent).toBe('Instructions');
      expect(rowText(instructions).value).toBe('Keep the tone friendly.\nCite every source.');
      expect(within(instructions).getByText('Changes to the instructions')).toBeTruthy();
      const diff = rowDiffBody(instructions);
      expect(diff.querySelector('.memory-suggestion-diff-del')?.textContent).toBe('formal.');
      expect(diff.querySelector('.memory-suggestion-diff-add')?.textContent).toBe('friendly.');
      expect(within(instructions).getByText('The user asked for a lighter tone.')).toBeTruthy();
      expect(within(instructions).getByText('42 / 4,000')).toBeTruthy();

      // The memory row: its pill and text, no diff.
      const memory = suggestionRow('Doc memory text');
      expect(memory.querySelector('.memory-suggestion-pill')?.textContent).toBe('Memory');
      expect(rowText(memory).value).toBe('The board meets weekly.');
      expect(memory.querySelector('.memory-suggestion-diff')).toBeNull();

      // The panel is the doc: no link or title inside it.
      expect(within(block).queryByRole('link')).toBeNull();
      expect(block.querySelector('.memory-suggestion-doc-link')).toBeNull();
      expect(block.querySelector('.memory-suggestion-doc-unavailable')).toBeNull();
      expect(within(block).queryByText('Quarterly plan')).toBeNull();

      // Above the empty line, which still says the doc has no notes yet,
      // then Instructions.
      const emptyLine = screen.getByText(EDITOR_LINE);
      const heading = screen.getByRole('heading', { name: 'Instructions' });
      expect(block.compareDocumentPosition(emptyLine) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(emptyLine.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });

    it('accept sends the listed version unedited, and the edited text with it when changed', async () => {
      const view = withSuggestions([INSTRUCTIONS_SUGGESTION, MEMORY_SUGGESTION]);
      const accept = vi.mocked(view.acceptSuggestion);
      accept.mockResolvedValue(acceptResponse(INSTRUCTIONS_SUGGESTION));
      renderPanel(view);

      fireEvent.click(rowButton(suggestionRow('Doc instructions text'), 'Accept'));
      expect(accept).toHaveBeenLastCalledWith('s1', { expectedInstructionsVersion: 3 });
      await waitFor(() =>
        expect(rowButton(suggestionRow('Doc instructions text'), 'Accept').disabled).toBe(false),
      );

      // Edited: the trimmed text, still with the version.
      const instructions = suggestionRow('Doc instructions text');
      fireEvent.change(rowText(instructions), {
        target: { value: '  Keep the tone warm.\nCite every source.  ' },
      });
      fireEvent.click(rowButton(instructions, 'Accept'));
      expect(accept).toHaveBeenLastCalledWith('s1', {
        content: 'Keep the tone warm.\nCite every source.',
        expectedInstructionsVersion: 3,
      });
      await waitFor(() => expect(rowButton(instructions, 'Accept').disabled).toBe(false));

      // A memory row has no version to send.
      const memory = suggestionRow('Doc memory text');
      fireEvent.click(rowButton(memory, 'Accept'));
      expect(accept).toHaveBeenLastCalledWith('s2', {});
      await waitFor(() => expect(rowButton(memory, 'Accept').disabled).toBe(false));
      fireEvent.change(rowText(memory), { target: { value: 'The board meets monthly.' } });
      fireEvent.click(rowButton(memory, 'Accept'));
      expect(accept).toHaveBeenLastCalledWith('s2', { content: 'The board meets monthly.' });
      await waitFor(() => expect(rowButton(memory, 'Accept').disabled).toBe(false));
      expect(view.dismissSuggestion).not.toHaveBeenCalled();
    });

    it('a row is busy while its call is in flight, and its draft goes with it when it leaves', async () => {
      const view = withSuggestions([INSTRUCTIONS_SUGGESTION, MEMORY_SUGGESTION]);
      const pending = deferred<AcceptDocNoteSuggestionResponse>();
      vi.mocked(view.acceptSuggestion).mockReturnValue(pending.promise);
      const { rerenderView } = renderPanel(view);

      const memory = suggestionRow('Doc memory text');
      fireEvent.change(rowText(memory), { target: { value: 'The board meets monthly.' } });
      fireEvent.click(rowButton(memory, 'Accept'));
      expect(rowButton(memory, 'Accept').disabled).toBe(true);
      expect(rowButton(memory, 'Dismiss').disabled).toBe(true);
      expect(rowText(memory).disabled).toBe(true);
      // The other row stays live.
      expect(rowButton(suggestionRow('Doc instructions text'), 'Accept').disabled).toBe(false);

      // As the hook does: the row leaves the list, then the call resolves.
      rerenderView({ ...view, suggestions: [INSTRUCTIONS_SUGGESTION] });
      await act(async () => {
        pending.resolve(acceptResponse(MEMORY_SUGGESTION));
      });
      expect(screen.queryByRole('textbox', { name: 'Doc memory text' })).toBeNull();
      expect(suggestionsBlock().querySelector('.doc-notes-count')?.textContent).toBe('1');

      // Listed again (say, a re-fetch raced the accept): the proposal, not the old draft.
      rerenderView({ ...view, suggestions: [INSTRUCTIONS_SUGGESTION, MEMORY_SUGGESTION] });
      expect(rowText(suggestionRow('Doc memory text')).value).toBe('The board meets weekly.');
      expect(rowButton(suggestionRow('Doc memory text'), 'Accept').disabled).toBe(false);

      // The last row gone: no block.
      rerenderView({ ...view, suggestions: [] });
      expect(screen.queryByRole('heading', { name: 'Suggestions for you' })).toBeNull();
    });

    it('dismiss calls view.dismissSuggestion', async () => {
      const view = withSuggestions([INSTRUCTIONS_SUGGESTION, MEMORY_SUGGESTION]);
      vi.mocked(view.dismissSuggestion).mockResolvedValue({ ...MEMORY_SUGGESTION, status: 'dismissed' });
      renderPanel(view);

      const memory = suggestionRow('Doc memory text');
      fireEvent.click(rowButton(memory, 'Dismiss'));
      expect(view.dismissSuggestion).toHaveBeenCalledWith('s2');
      expect(view.acceptSuggestion).not.toHaveBeenCalled();
      await waitFor(() => expect(rowButton(memory, 'Dismiss').disabled).toBe(false));
      expect(within(suggestionsBlock()).queryByRole('alert')).toBeNull();
    });

    it('a stale 409 re-bases the diff on its current text until the listed row is as new', async () => {
      const view = withSuggestions([INSTRUCTIONS_SUGGESTION]);
      const accept = vi.mocked(view.acceptSuggestion);
      accept.mockRejectedValueOnce(
        staleError(
          { instructions: 'Keep the tone casual.\nCite every source.', instructions_version: 5 },
          ACCEPT_STALE_MESSAGE,
        ),
      );
      const { container, rerenderView } = renderPanel(view);
      const draft = 'Keep the tone friendly, always.\nCite every source.';
      fireEvent.change(rowText(suggestionRow('Doc instructions text')), { target: { value: draft } });
      fireEvent.click(rowButton(suggestionRow('Doc instructions text'), 'Accept'));
      expect(accept).toHaveBeenLastCalledWith('s1', { content: draft, expectedInstructionsVersion: 3 });

      const alert = await within(suggestionsBlock()).findByRole('alert');
      expect(alert.textContent).toBe(ACCEPT_STALE_MESSAGE);
      // The draft is kept; the diff now starts from the current text, and the
      // instructions moved past the proposal's base.
      let row = suggestionRow('Doc instructions text');
      expect(rowText(row).value).toBe(draft);
      expect(rowDiffBody(row).querySelector('.memory-suggestion-diff-del')?.textContent).toBe('casual.');
      expect(row.querySelector('.memory-suggestion-moved')?.textContent).toBe(
        'The instructions changed since this was suggested',
      );
      // Only the row's text: the notes' own instructions are the hook's.
      expect(container.querySelector('.doc-notes-instructions-text')?.textContent).toBe(
        'Keep the tone formal.\nCite every source.',
      );

      // A listed row older than the 409 does not undo the re-base.
      rerenderView({ ...view, suggestions: [makeSuggestion()] });
      row = suggestionRow('Doc instructions text');
      expect(rowDiffBody(row).querySelector('.memory-suggestion-diff-del')?.textContent).toBe('casual.');

      accept.mockReturnValueOnce(new Promise(() => {}));
      fireEvent.click(rowButton(row, 'Accept'));
      expect(accept).toHaveBeenLastCalledWith('s1', { content: draft, expectedInstructionsVersion: 5 });
      cleanup();

      // The hook's re-fetched row, as new or newer, takes over.
      const refetched = makeSuggestion({
        current_instructions: 'Keep the tone serious.\nCite every source.',
        current_instructions_version: 6,
        instructions_moved: true,
      });
      const next = withSuggestions([INSTRUCTIONS_SUGGESTION]);
      vi.mocked(next.acceptSuggestion)
        .mockRejectedValueOnce(
          staleError({ instructions: 'Keep the tone casual.\nCite every source.', instructions_version: 5 }),
        )
        .mockReturnValueOnce(new Promise(() => {}));
      const second = renderPanel(next);
      fireEvent.click(rowButton(suggestionRow('Doc instructions text'), 'Accept'));
      await within(suggestionsBlock()).findByRole('alert');
      second.rerenderView({ ...next, suggestions: [refetched] });
      row = suggestionRow('Doc instructions text');
      expect(rowDiffBody(row).querySelector('.memory-suggestion-diff-del')?.textContent).toBe('serious.');
      // The message stays until the next action.
      expect(within(row).getByRole('alert').textContent).toBe(STALE_MESSAGE);
      fireEvent.click(rowButton(row, 'Accept'));
      expect(next.acceptSuggestion).toHaveBeenLastCalledWith('s1', { expectedInstructionsVersion: 6 });
      expect(within(row).queryByRole('alert')).toBeNull();
    });

    it('a stale 409 without a current text keeps the listed row and shows the message', async () => {
      const view = withSuggestions([INSTRUCTIONS_SUGGESTION]);
      vi.mocked(view.acceptSuggestion)
        .mockRejectedValueOnce(staleError(null, ACCEPT_STALE_MESSAGE))
        .mockReturnValueOnce(new Promise(() => {}));
      const { rerenderView } = renderPanel(view);
      fireEvent.click(rowButton(suggestionRow('Doc instructions text'), 'Accept'));
      expect((await within(suggestionsBlock()).findByRole('alert')).textContent).toBe(
        ACCEPT_STALE_MESSAGE,
      );
      const row = suggestionRow('Doc instructions text');
      expect(rowDiffBody(row).querySelector('.memory-suggestion-diff-del')?.textContent).toBe('formal.');

      // The hook re-fetches the suggestions after a 409; its row carries the version.
      rerenderView({
        ...view,
        suggestions: [makeSuggestion({ current_instructions_version: 8, current_instructions: 'New.' })],
      });
      fireEvent.click(rowButton(suggestionRow('Doc instructions text'), 'Accept'));
      expect(view.acceptSuggestion).toHaveBeenLastCalledWith('s1', { expectedInstructionsVersion: 8 });
    });

    it.each([
      ['accept', 'suggestion_resolved', 409],
      ['accept', 'suggestion_not_found', 404],
      ['dismiss', 'suggestion_resolved', 409],
      ['dismiss', 'suggestion_not_found', 404],
    ] as const)('%s answered %s shows no error (the hook drops the row)', async (action, code, status) => {
      const view = withSuggestions([MEMORY_SUGGESTION]);
      const err = new ApiClientError('Gone', status, code);
      if (action === 'accept') vi.mocked(view.acceptSuggestion).mockRejectedValueOnce(err);
      else vi.mocked(view.dismissSuggestion).mockRejectedValueOnce(err);
      renderPanel(view);
      const row = suggestionRow('Doc memory text');
      fireEvent.click(rowButton(row, action === 'accept' ? 'Accept' : 'Dismiss'));
      await waitFor(() => expect(rowButton(row, 'Accept').disabled).toBe(false));
      expect(within(row).queryByRole('alert')).toBeNull();
    });

    it('shows other errors on the row, with a fallback, and keeps the draft', async () => {
      const view = withSuggestions([INSTRUCTIONS_SUGGESTION, MEMORY_SUGGESTION]);
      vi.mocked(view.acceptSuggestion)
        .mockRejectedValueOnce(
          new ApiClientError('Only the doc owner can change its instructions.', 403, 'forbidden'),
        )
        .mockRejectedValueOnce(new ApiClientError('', 500));
      vi.mocked(view.dismissSuggestion).mockRejectedValueOnce(new ApiClientError('', 500));
      renderPanel(view);

      const instructions = suggestionRow('Doc instructions text');
      fireEvent.change(rowText(instructions), { target: { value: 'Keep the tone warm.' } });
      fireEvent.click(rowButton(instructions, 'Accept'));
      expect((await within(instructions).findByRole('alert')).textContent).toBe(
        'Only the doc owner can change its instructions.',
      );
      expect(rowText(instructions).value).toBe('Keep the tone warm.');
      expect(rowButton(instructions, 'Accept').disabled).toBe(false);

      const memory = suggestionRow('Doc memory text');
      fireEvent.click(rowButton(memory, 'Dismiss'));
      expect((await within(memory).findByRole('alert')).textContent).toBe('Failed to dismiss');
      // Each row keeps its own error.
      expect(within(instructions).getByRole('alert')).toBeTruthy();

      fireEvent.click(rowButton(memory, 'Accept'));
      await waitFor(() =>
        expect(within(memory).getByRole('alert').textContent).toBe('Failed to save doc note'),
      );
    });

    it('read share: the block shows the viewer\'s own rows, with the no-notes line', () => {
      const { container } = renderPanel(withSuggestions([MEMORY_SUGGESTION], { ...EMPTY, ...READ_SHARE }));
      expect(suggestionRows()).toHaveLength(1);
      expect(rowButton(suggestionRow('Doc memory text'), 'Accept')).toBeTruthy();
      expect(screen.getByText('This doc has no notes.')).toBeTruthy();
      expect(screen.queryByRole('heading', { name: 'Instructions' })).toBeNull();
      expect(screen.queryByRole('heading', { name: 'Memories' })).toBeNull();
      // The rows are focusable: the scroller is no tab stop of its own.
      expect(container.querySelector('.doc-notes-scroll')?.hasAttribute('tabindex')).toBe(false);
    });

    it('details: the block is inside the collapsed section', () => {
      const { container } = renderPanel(withSuggestions([MEMORY_SUGGESTION]), { variant: 'details' });
      const body = container.querySelector('details > .doc-notes-details-body') as HTMLElement;
      expect(body.querySelector('.doc-notes-suggestions .memory-suggestion-row--doc')).not.toBeNull();
    });
  });

  describe('variants', () => {
    it('card: a region named "Notes" with the right-panel card chrome', () => {
      const { container } = renderPanel(makeView());
      const card = screen.getByRole('region', { name: 'Notes' });
      expect(card.classList.contains('right-panel-card')).toBe(true);
      expect(within(card).getByRole('heading', { name: 'Notes' })).toBeTruthy();
      expect(container.querySelector('details')).toBeNull();
    });

    it('details: a closed section titled "Notes" with the same contents inside it', () => {
      const { container } = renderPanel(makeView(), { variant: 'details' });
      const details = container.querySelector('details') as HTMLDetailsElement;
      expect(details.hasAttribute('open')).toBe(false);
      expect(details.querySelector('summary')?.textContent).toBe('Notes');
      expect(container.querySelector('.right-panel-card')).toBeNull();
      expect(screen.queryByRole('region', { name: 'Notes' })).toBeNull();

      // Everything is rendered inside the <details>, under the summary.
      const body = details.querySelector(':scope > .doc-notes-details-body') as HTMLElement;
      expect(body.querySelector('.doc-notes-instructions-text')?.textContent).toBe(
        'Keep the tone formal.\nCite every source.',
      );
      const hidden = { hidden: true };
      expect(within(body).getByRole('button', { name: 'Edit instructions', ...hidden })).toBeTruthy();
      expect(within(body).getByRole('textbox', { name: 'New memory', ...hidden })).toBeTruthy();
      expect(within(body).getAllByRole('button', { name: 'Delete memory', ...hidden })).toHaveLength(2);
      expect(container.querySelectorAll('.doc-notes-body')).toHaveLength(1);
    });
  });
});
