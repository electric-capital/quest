// DocsSection: the mode badge on each row follows utils/docMode -- a private
// doc's "Private" badge only while `showPrivateBadge` (the public_projects
// gate is open for the user), a public doc's badge always.
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
    access: { can_rename: true, can_switch_mode: true, can_delete: true, write: 'free' },
    ...overrides,
  };
}

const DOCS = [doc('d1'), doc('d2', { mode: 'public' }), doc('d3')];

function renderSection(showPrivateBadge: boolean) {
  return render(
    <DocsSection
      docs={DOCS}
      hasMore={false}
      loading={false}
      activeDocId={null}
      onOpenDoc={vi.fn()}
      onOpenAll={vi.fn()}
      showPrivateBadge={showPrivateBadge}
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

  it('shows no badge on private rows when showPrivateBadge is false, but keeps the public one', () => {
    const { container } = renderSection(false);
    expect(rowBadges(container)).toEqual([
      ['Doc d1', null],
      ['Doc d2', 'Public'],
      ['Doc d3', null],
    ]);
  });

  it('badges every row when showPrivateBadge is true', () => {
    const { container } = renderSection(true);
    expect(rowBadges(container)).toEqual([
      ['Doc d1', 'Private'],
      ['Doc d2', 'Public'],
      ['Doc d3', 'Private'],
    ]);
  });
});
