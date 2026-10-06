// DocsSection: the mode badge on each row follows utils/docMode -- a public
// doc's row carries the "Public" badge, a private doc's row none.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import type { Doc } from '../../api/types';
import { DocsSection } from './DocsSection';

function doc(id: string, overrides: Partial<Doc> = {}): Doc {
  return {
    id,
    owner_id: 1,
    project_id: null,
    title: `Doc ${id}`,
    description: '',
    mode: 'private',
    content_size: 0,
    asset_count: 0,
    last_write_source: null,
    created_at: '2026-10-01T00:00:00',
    // Distinct times so the newest-first row order is fixed: d1, d2, d3.
    updated_at: `2026-10-0${4 - Number(id.slice(1))}T00:00:00`,
    scope: 'user',
    shared: false,
    shared_with_me: false,
    permission: null,
    owner: null,
    last_write_user: null,
    access: { can_rename: true, can_switch_mode: false, can_delete: true, write: 'free', can_edit: true, can_share: true, can_delete_assets: true },
    ...overrides,
  };
}

function renderSection(docs: Doc[]) {
  return render(
    <DocsSection
      docs={docs}
      loading={false}
      activeDocId={null}
      onOpenDoc={vi.fn()}
      onOpenAll={vi.fn()}
    />,
  );
}

/** Each row's title with the badge it shows (null = none). */
function rowBadges(container: HTMLElement): [string | null | undefined, string | null][] {
  return [...container.querySelectorAll('.doc-row')].map((row) => [
    row.querySelector('.doc-row-title')?.textContent,
    row.querySelector('.doc-mode-badge')?.textContent ?? null,
  ]);
}

describe('DocsSection', () => {
  afterEach(() => {
    cleanup();
  });

  it('badges only the public row', () => {
    const { container } = renderSection([
      doc('d1'),
      doc('d2', { project_id: 'p1', scope: 'project', mode: 'public' }),
      doc('d3'),
    ]);
    expect(rowBadges(container)).toEqual([
      ['Doc d1', null],
      ['Doc d2', 'Public'],
      ['Doc d3', null],
    ]);
  });

  it('shows no badge at all when every row is private', () => {
    const { container } = renderSection([doc('d1'), doc('d2'), doc('d3')]);
    expect(rowBadges(container)).toEqual([
      ['Doc d1', null],
      ['Doc d2', null],
      ['Doc d3', null],
    ]);
    expect(container.querySelector('.doc-mode-badge')).toBeNull();
  });
});
