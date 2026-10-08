/**
 * The one way to download a workspace file from a component: the hidden-data
 * acknowledgement (contexts/DownloadWarningContext.tsx), then the fetch of
 * whichever copy the user chose -- the file as stored, or the server's
 * metadata-stripped rewrite of a raster image -- then the browser save.
 * Resolves true when a file was handed to the browser and false when the
 * user cancelled the warning; fetch errors propagate so a caller with its
 * own error surface (useFileBrowser) can show them.
 */

import { useCallback } from 'react';
import { downloadFile, saveBlobToDisk } from '../api/fileApi';
import { useDownloadWarning } from '../contexts/DownloadWarningContext';

export function useWorkspaceDownload(): (conversationId: string, filePath: string) => Promise<boolean> {
  const { confirmDownload } = useDownloadWarning();
  return useCallback(async (conversationId: string, filePath: string) => {
    const name = filePath.split('/').pop() || filePath;
    const decision = await confirmDownload({ name, kind: 'file' });
    if (decision === 'cancel') return false;
    saveBlobToDisk(await downloadFile(conversationId, filePath, decision));
    return true;
  }, [confirmDownload]);
}
