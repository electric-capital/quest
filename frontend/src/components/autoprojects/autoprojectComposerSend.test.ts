// A composer submission becoming a run: model, thinking level and skills go
// to startAutoProjectRun, queued files and pasted images upload to the new run
// conversation before the send; a partial failure warns, an image upload
// that loses every image aborts the start.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  initialUploadStatus,
  startAutoProjectRunFromComposer,
  type AutoProjectComposerSubmission,
} from './autoprojectComposerSend';

const mocks = vi.hoisted(() => ({
  startAutoProjectRun: vi.fn(),
  uploadFiles: vi.fn(),
  uploadComposerAttachments: vi.fn(),
}));

vi.mock('../../services/autoprojectRuns', () => ({ startAutoProjectRun: mocks.startAutoProjectRun }));
vi.mock('../../api/fileApi', () => ({
  uploadFiles: mocks.uploadFiles,
  uploadComposerAttachments: mocks.uploadComposerAttachments,
}));

function submission(extra: Partial<AutoProjectComposerSubmission> = {}): AutoProjectComposerSubmission {
  return { text: 'Do it', model: null, thinkingEffort: null, skillIds: [], files: [], imageFiles: [], ...extra };
}

type BeforeSend = (conversationId: string) => Promise<{ attachedFilenames: string[]; attachments: unknown[] }>;

/** The beforeSend handed to startAutoProjectRun by the last call. */
function beforeSend(): BeforeSend | undefined {
  return mocks.startAutoProjectRun.mock.calls.at(-1)?.[1].beforeSend;
}

beforeEach(() => {
  mocks.startAutoProjectRun.mockReset().mockResolvedValue({ conversationId: 'c1', seq: 1 });
  mocks.uploadFiles.mockReset();
  mocks.uploadComposerAttachments.mockReset();
});

describe('startAutoProjectRunFromComposer', () => {
  it('passes the prompt, model, thinking level and skills, with no upload step without attachments', async () => {
    await startAutoProjectRunFromComposer('ap1', submission({ model: 'm', thinkingEffort: 'low', skillIds: ['s'] }));
    expect(mocks.startAutoProjectRun).toHaveBeenCalledWith('ap1', {
      message: 'Do it', model: 'm', thinkingEffort: 'low', skillIds: ['s'], beforeSend: undefined,
    });
  });

  it('uploads files to the run conversation and sends their names, warning on partial failure', async () => {
    const file = new File(['x'], 'a.csv');
    mocks.uploadFiles.mockResolvedValue({
      uploadedFiles: [{ name: 'a.csv' }],
      errors: [{ message: 'b.bin is too large' }],
    });
    const onWarning = vi.fn();
    const onUploadStatus = vi.fn();
    await startAutoProjectRunFromComposer('ap1', submission({ files: [file] }), { onWarning, onUploadStatus });
    const result = await beforeSend()!('c1');
    expect(mocks.uploadFiles.mock.calls[0].slice(0, 3)).toEqual(['c1', [file], '']);
    expect(result).toEqual({ attachedFilenames: ['a.csv'], attachments: [] });
    expect(onWarning).toHaveBeenCalledWith('Some files could not be uploaded: b.bin is too large');
    expect(onUploadStatus).toHaveBeenLastCalledWith(null);
  });

  it('sends the refs of pasted images and warns when only some were rejected', async () => {
    const image = new File(['i'], 'p.png', { type: 'image/png' });
    mocks.uploadComposerAttachments.mockResolvedValue({
      attachments: [{ id: 'att1' }],
      errors: [{ message: 'second image unreadable' }],
    });
    const onWarning = vi.fn();
    await startAutoProjectRunFromComposer('ap1', submission({ imageFiles: [image, image] }), { onWarning });
    const result = await beforeSend()!('c1');
    expect(mocks.uploadComposerAttachments).toHaveBeenCalledWith('c1', [image, image]);
    expect(result.attachments).toEqual([{ id: 'att1' }]);
    expect(onWarning).toHaveBeenCalledWith('Some pasted images could not be uploaded: second image unreadable');
  });

  it('aborts the start when every pasted image was rejected', async () => {
    mocks.uploadComposerAttachments.mockResolvedValue({ attachments: [], errors: [{ message: 'bad image' }] });
    await startAutoProjectRunFromComposer('ap1', submission({ imageFiles: [new File(['i'], 'p.png')] }));
    await expect(beforeSend()!('c1')).rejects.toThrow('Pasted images could not be uploaded: bad image');
  });

  it('shows an upload strip from Send only when something is queued', () => {
    expect(initialUploadStatus(submission())).toBeNull();
    expect(initialUploadStatus(submission({ files: [new File(['x'], 'a.csv')] }))).toMatchObject({ percent: null });
    expect(initialUploadStatus(submission({ imageFiles: [new File(['x'], 'p.png')] }))).toMatchObject({ percent: null });
  });
});
