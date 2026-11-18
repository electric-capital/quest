/**
 * The one way to download a workspace file from a component (of any file
 * space: a `FileTarget`, a bare string being a conversation id): the hidden-data
 * acknowledgement (contexts/DownloadWarningContext.tsx), then the fetch of
 * whichever copy the user chose -- the file as stored, or the server's
 * metadata-stripped rewrite of a raster image -- then the browser save.
 * Resolves true when a file was handed to the browser and false when the
 * user cancelled the warning; fetch errors propagate so a caller with its
 * own error surface (useFileBrowser) can show them.
 */

import { useCallback } from 'react';
import { downloadFile, saveBlobToDisk, type FileTarget } from '../api/fileApi';
import { useDownloadWarning } from '../contexts/DownloadWarningContext';

export function useWorkspaceDownload(): (target: FileTarget, filePath: string) => Promise<boolean> {
  const { confirmDownload } = useDownloadWarning();
  return useCallback(async (target: FileTarget, filePath: string) => {
    const name = filePath.split('/').pop() || filePath;
    const decision = await confirmDownload({ name, kind: 'file' });
    if (decision === 'cancel') return false;
    saveBlobToDisk(await downloadFile(target, filePath, decision));
    return true;
  }, [confirmDownload]);
}
