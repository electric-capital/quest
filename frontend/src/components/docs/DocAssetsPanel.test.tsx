// DocAssetsPanel: one row per asset (thumbnail from the doc asset route, name,
// humanized size), the empty state, the thumbnail fallback, and a row click
// opening DocImageLightbox on the full image with a Download link.
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { DocAsset } from '../../api/types';
import { DocAssetsPanel } from './DocAssetsPanel';

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
});
