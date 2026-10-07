// The write_doc add_image card preview: workspace thumbnail, caption,
// appended markdown line, broken-file fallback chip and the full-size
// viewer -- plus the `doc_image` branch of ActionRequestPreviewFields that
// mounts it in both card renderers.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { DocImagePreview as DocImagePreviewData, PreviewField } from '../api/types';
import { DownloadWarningProvider } from '../contexts/DownloadWarningContext';
import { ActionRequestPreviewFields } from './ActionRequestPreviewFields';
import { DocImagePreview } from './DocImagePreview';

// The real modal pulls in the PDF viewer; a stub records what it was given.
vi.mock('./FileViewerModal', () => ({
  FileViewerModal: (props: { conversationId: string; filePath: string; isImage?: boolean }) => (
    <div data-testid="file-viewer">
      {props.conversationId}|{props.filePath}|{String(props.isImage)}
    </div>
  ),
}));

vi.mock('../api/fileApi', () => ({
  downloadFile: vi.fn(),
  saveBlobToDisk: vi.fn(),
}));

// Not under test here; keeps FileBrowser's import graph out of the run.
vi.mock('./SubagentReturnFilesPreview', () => ({
  SubagentReturnFilesPreview: () => null,
}));

const IMAGE: DocImagePreviewData = {
  workspace_path: 'charts/q3 sales.png',
  asset_name: 'q3-sales.png',
  markdown: '![Q3 sales](assets/q3-sales.png)',
  size_bytes: 43110,
};

const EXPECTED_SRC = '/app/api/conversations/conv-1/files/download?path=charts%2Fq3%20sales.png';

// The download path needs the hidden-data warning provider above it.
const renderWithProviders = (ui: React.ReactElement) =>
  render(<DownloadWarningProvider>{ui}</DownloadWarningProvider>);

describe('DocImagePreview', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders the workspace thumbnail, caption and appended line', () => {
    renderWithProviders(<DocImagePreview image={IMAGE} conversationId="conv-1" />);

    const img = screen.getByRole('img', { name: 'q3-sales.png' });
    expect(img.getAttribute('src')).toBe(EXPECTED_SRC);
    expect(img.getAttribute('loading')).toBe('lazy');

    expect(screen.getByText('q3-sales.png')).toBeTruthy();
    expect(screen.getByText('42.1 KB')).toBeTruthy();
    expect(screen.getByText('assets/q3-sales.png')).toBeTruthy();
    expect(screen.getByText('Appended:')).toBeTruthy();
    const code = screen.getByText(IMAGE.markdown);
    expect(code.tagName).toBe('CODE');
  });

  it('omits the size when unknown and the markdown block when empty', () => {
    renderWithProviders(
      <DocImagePreview
        image={{ ...IMAGE, size_bytes: null, markdown: '' }}
        conversationId="conv-1"
      />,
    );
    expect(screen.queryByText(/KB|bytes|\bB\b/)).toBeNull();
    expect(screen.queryByText('Appended:')).toBeNull();
  });

  it('labels the markdown line as a reference when the image is not placed', () => {
    renderWithProviders(<DocImagePreview image={IMAGE} conversationId="conv-1" placed={false} />);
    expect(screen.queryByText('Appended:')).toBeNull();
    expect(screen.getByText('Markdown:')).toBeTruthy();
  });

  it('falls back to a path chip when the image fails to load', () => {
    renderWithProviders(<DocImagePreview image={IMAGE} conversationId="conv-1" />);
    fireEvent.error(screen.getByRole('img', { name: 'q3-sales.png' }));

    expect(screen.queryByRole('img')).toBeNull();
    const chip = screen.getByText('charts/q3 sales.png');
    expect(chip.tagName).toBe('CODE');
    expect(chip.getAttribute('title')).toBe('charts/q3 sales.png');
    // The caption stays.
    expect(screen.getByText('42.1 KB')).toBeTruthy();
  });

  it('opens the workspace file in the file viewer on click', () => {
    renderWithProviders(<DocImagePreview image={IMAGE} conversationId="conv-1" />);
    expect(screen.queryByTestId('file-viewer')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'q3-sales.png' }));
    expect(screen.getByTestId('file-viewer').textContent).toBe('conv-1|charts/q3 sales.png|true');
  });
});

describe('ActionRequestPreviewFields doc_image branch', () => {
  afterEach(() => {
    cleanup();
  });

  const field: PreviewField = {
    key: 'Image',
    value: 'q3-sales.png (42.1 KB)',
    type: 'doc_image',
    image: IMAGE,
  };

  it.each([
    ['action-request-preview' as const],
    ['request-preview' as const],
  ])('renders the key line and the image preview (%s)', (classPrefix) => {
    const { container } = renderWithProviders(
      <ActionRequestPreviewFields
        previewFields={[{ key: 'Doc', value: 'Roadmap' }, field]}
        params={{ operation: 'add_image', doc_id: 'd1', placement: 'append' }}
        conversationId="conv-1"
        classPrefix={classPrefix}
      />,
    );
    const row = container.querySelector(`.${classPrefix}-field.doc-image-field`);
    expect(row).toBeTruthy();
    // Key only: the image caption below carries the name and size.
    expect(row?.querySelector(`.${classPrefix}-key`)?.textContent).toBe('Image:');
    expect(screen.getByRole('img', { name: 'q3-sales.png' }).getAttribute('src')).toBe(EXPECTED_SRC);
    expect(screen.getByText('Appended:')).toBeTruthy();
  });

  it('reads placement "none" from the request params', () => {
    renderWithProviders(
      <ActionRequestPreviewFields
        previewFields={[field]}
        params={{ operation: 'add_image', doc_id: 'd1', placement: 'none' }}
        conversationId="conv-1"
        classPrefix="action-request-preview"
      />,
    );
    expect(screen.getByText('Markdown:')).toBeTruthy();
  });

  it('falls back to the plain value without an image payload', () => {
    renderWithProviders(
      <ActionRequestPreviewFields
        previewFields={[{ key: 'Image', value: 'charts/q3.png', type: 'doc_image' }]}
        params={{}}
        conversationId="conv-1"
        classPrefix="request-preview"
      />,
    );
    expect(screen.getByText('charts/q3.png')).toBeTruthy();
    expect(screen.queryByRole('img')).toBeNull();
  });
});
