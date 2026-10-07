// DocAssetsPanel: one row per asset (thumbnail from the doc asset route, name,
// humanized size), the empty state, the thumbnail fallback, a row click
// opening DocImageLightbox on the full image with a Download link, and the
// owner's per-image Delete (confirm, asset_in_use, other errors).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ApiClientError } from '../../api/request';
import type { DocAsset } from '../../api/types';
import { DocAssetsPanel } from './DocAssetsPanel';

const mocks = vi.hoisted(() => ({
  deleteDocAsset: vi.fn(),
}));

vi.mock('../../api/docsApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/docsApi')>();
  return { ...actual, deleteDocAsset: mocks.deleteDocAsset };
});

const ASSETS: DocAsset[] = [
  { name: 'chart.png', size: 1229, mime: 'image/png' },
  { name: 'my photo.jpg', size: 340, mime: 'image/jpeg' },
  { name: 'scan.webp', size: 2 * 1024 * 1024, mime: 'image/webp' },
];

function rows(container: HTMLElement) {
  return [...container.querySelectorAll('.doc-asset-row')].map((row) => ({
    name: row.querySelector('.doc-asset-name')?.textContent,
    size: row.querySelector('.doc-asset-size')?.textContent,
    src: row.querySelector('img')?.getAttribute('src') ?? null,
  }));
}

describe('DocAssetsPanel', () => {
  beforeEach(() => {
    mocks.deleteDocAsset.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it('renders a card with the heading, count and one row per asset', () => {
    const { container } = render(<DocAssetsPanel docId="d1" assets={ASSETS} />);

    const card = screen.getByRole('region', { name: 'Assets' });
    expect(card.classList.contains('right-panel-card')).toBe(true);
    expect(within(card).getByRole('heading', { name: 'Assets' })).toBeTruthy();
    expect(card.querySelector('.doc-assets-count')?.textContent).toBe('3');
    expect(rows(container)).toEqual([
      { name: 'chart.png', size: '1.2 KB', src: '/app/api/docs/d1/assets/chart.png' },
      { name: 'my photo.jpg', size: '340 B', src: '/app/api/docs/d1/assets/my%20photo.jpg' },
      { name: 'scan.webp', size: '2.0 MB', src: '/app/api/docs/d1/assets/scan.webp' },
    ]);
    // Thumbnails load lazily and are decorative (the row names the file).
    const img = container.querySelector('.doc-asset-thumb img');
    expect(img?.getAttribute('loading')).toBe('lazy');
    expect(img?.getAttribute('alt')).toBe('');
    expect(screen.queryByText('No images in this doc.')).toBeNull();
  });

  it('shows the empty state when the doc has no images', () => {
    const { container } = render(<DocAssetsPanel docId="d1" assets={[]} />);
    expect(screen.getByText('No images in this doc.')).toBeTruthy();
    expect(container.querySelector('.doc-assets-count')?.textContent).toBe('0');
    expect(container.querySelectorAll('.doc-asset-row')).toHaveLength(0);
  });

  it('falls back to an icon when a thumbnail fails to load', () => {
    const { container } = render(<DocAssetsPanel docId="d1" assets={ASSETS} />);
    const first = container.querySelector('.doc-asset-row') as HTMLElement;
    fireEvent.error(first.querySelector('img')!);
    expect(first.querySelector('img')).toBeNull();
    expect(first.querySelector('.doc-asset-thumb svg')).not.toBeNull();
    // The other thumbnails are untouched.
    expect(container.querySelectorAll('.doc-asset-thumb img')).toHaveLength(2);
  });

  it('opens the lightbox on the clicked image, with name, size and a Download link', () => {
    render(<DocAssetsPanel docId="d1" assets={ASSETS} />);
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /my photo\.jpg/ }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('heading').textContent).toBe('my photo.jpg');
    expect(within(dialog).getByText('340 B')).toBeTruthy();
    expect(within(dialog).getByRole('img', { name: 'my photo.jpg' }).getAttribute('src')).toBe(
      '/app/api/docs/d1/assets/my%20photo.jpg',
    );
    const download = within(dialog).getByRole('link', { name: 'Download' });
    expect(download.getAttribute('href')).toBe('/app/api/docs/d1/assets/my%20photo.jpg');
    expect(download.getAttribute('download')).toBe('my photo.jpg');

    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('the details variant is a collapsed "Assets (N)" section with the same rows', () => {
    const { container } = render(<DocAssetsPanel docId="d1" assets={ASSETS} variant="details" />);
    const details = container.querySelector('details') as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details.querySelector('summary')?.textContent).toBe('Assets (3)');
    expect(container.querySelector('.right-panel-card')).toBeNull();
    expect(rows(container).map((row) => row.name)).toEqual(['chart.png', 'my photo.jpg', 'scan.webp']);
  });
  describe('delete', () => {
    const DELETED = {
      deleted: true,
      asset_count: 2,
      require_approval: false,
      updated_at: '2026-10-06T10:00:30',
      previous_updated_at: '2026-10-06T10:00:00',
    };

    it('offers no Delete without canDelete', () => {
      render(<DocAssetsPanel docId="d1" assets={ASSETS} />);
      expect(screen.queryByRole('button', { name: /^Delete / })).toBeNull();
    });

    it('confirms, deletes and reports the deleted image', async () => {
      mocks.deleteDocAsset.mockResolvedValue(DELETED);
      const onDeleted = vi.fn();
      render(<DocAssetsPanel docId="d1" assets={ASSETS} canDelete onDeleted={onDeleted} />);

      fireEvent.click(screen.getByRole('button', { name: 'Delete my photo.jpg' }));
      const dialog = screen.getByRole('dialog');
      expect(within(dialog).getByText("Delete image 'my photo.jpg'?")).toBeTruthy();
      expect(mocks.deleteDocAsset).not.toHaveBeenCalled();

      fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
      expect(mocks.deleteDocAsset).toHaveBeenCalledWith('d1', 'my photo.jpg');
      await waitFor(() => expect(onDeleted).toHaveBeenCalledWith('my photo.jpg', DELETED));
      expect(screen.queryByRole('dialog')).toBeNull();
    });

    it('Cancel in the confirm deletes nothing', () => {
      const onDeleted = vi.fn();
      render(<DocAssetsPanel docId="d1" assets={ASSETS} canDelete onDeleted={onDeleted} />);
      fireEvent.click(screen.getByRole('button', { name: 'Delete chart.png' }));
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(mocks.deleteDocAsset).not.toHaveBeenCalled();
    });

    it('explains asset_in_use inside the dialog', async () => {
      mocks.deleteDocAsset.mockRejectedValue(
        new ApiClientError('Asset is referenced by the doc body.', 409, 'asset_in_use'),
      );
      const onDeleted = vi.fn();
      render(<DocAssetsPanel docId="d1" assets={ASSETS} canDelete onDeleted={onDeleted} />);
      fireEvent.click(screen.getByRole('button', { name: 'Delete chart.png' }));
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));

      const alert = await within(screen.getByRole('dialog')).findByRole('alert');
      expect(alert.textContent).toBe('This image is used in the doc. Remove it from the text first.');
      expect(onDeleted).not.toHaveBeenCalled();
    });

    it('shows other failures inline', async () => {
      mocks.deleteDocAsset.mockRejectedValue(new ApiClientError('Server exploded', 500));
      render(<DocAssetsPanel docId="d1" assets={ASSETS} canDelete onDeleted={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: 'Delete chart.png' }));
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
      expect((await screen.findByRole('alert')).textContent).toBe('Server exploded');
    });

    it('the row still opens the lightbox when Delete is offered', () => {
      render(<DocAssetsPanel docId="d1" assets={ASSETS} canDelete onDeleted={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /^chart\.png/ }));
      expect(within(screen.getByRole('dialog')).getByRole('heading').textContent).toBe('chart.png');
    });

    it('the phone section offers Delete too', () => {
      render(
        <DocAssetsPanel docId="d1" assets={ASSETS} variant="details" canDelete onDeleted={vi.fn()} />,
      );
      expect(screen.getAllByRole('button', { name: /^Delete /, hidden: true })).toHaveLength(3);
    });
  });
});
