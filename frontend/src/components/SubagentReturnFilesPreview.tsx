/**
 * File list shown inside subagent_return approval cards. Each entry is a
 * workspace file of the SAME conversation the card is rendered in (the
 * subagent conversation), so preview/download go through the conversation's
 * own file APIs. Rendered by both card renderers (ActionRequestMessage and
 * RequestsView) via the `subagent_return_files` preview-field type.
 */

import React, { useState, useCallback } from 'react';
import { Download } from 'lucide-react';
import { useWorkspaceDownload } from '../hooks/useWorkspaceDownload';
import { getFileIconInfo } from '../utils/fileIcons';
import { FileViewerModal } from './FileViewerModal';
import { isImageFile, isJsonFile, isPdfFile, isCsvFile } from './FileBrowser';
import type { SubagentReturnFileEntry } from '../api/types';
import './SubagentReturnFilesPreview.css';

interface SubagentReturnFilesPreviewProps {
  files: SubagentReturnFileEntry[];
  conversationId: string;
}

function formatFileSize(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

export const SubagentReturnFilesPreview = React.memo(function SubagentReturnFilesPreview({
  files,
  conversationId,
}: SubagentReturnFilesPreviewProps) {
  const [viewerFile, setViewerFile] = useState<SubagentReturnFileEntry | null>(null);
  const downloadWorkspaceFile = useWorkspaceDownload();

  const handleDownload = useCallback(async (file: SubagentReturnFileEntry) => {
    try {
      await downloadWorkspaceFile(conversationId, file.path);
    } catch {
      // Swallow; the user can retry.
    }
  }, [downloadWorkspaceFile, conversationId]);

  return (
    <div className="subagent-return-files">
      {files.map((file) => {
        const { Icon, className } = getFileIconInfo(file.name);
        return (
          <div key={file.path} className="subagent-return-file-row">
            <Icon size={14} className={className} />
            <span className="subagent-return-file-name" title={file.path}>{file.name}</span>
            <span className="subagent-return-file-size">{formatFileSize(file.size_bytes)}</span>
            <button
              type="button"
              className="subagent-return-file-btn"
              onClick={() => setViewerFile(file)}
            >
              Preview
            </button>
            <button
              type="button"
              className="subagent-return-file-btn download"
              onClick={() => void handleDownload(file)}
              title={`Download ${file.name}`}
              aria-label={`Download ${file.name}`}
            >
              <Download size={13} />
            </button>
          </div>
        );
      })}
      {viewerFile && (
        <FileViewerModal
          isOpen={!!viewerFile}
          conversationId={conversationId}
          filePath={viewerFile.path}
          fileName={viewerFile.name}
          isImage={isImageFile(viewerFile.name)}
          isJson={isJsonFile(viewerFile.name)}
          isPdf={isPdfFile(viewerFile.name)}
          isCsv={isCsvFile(viewerFile.name)}
          onClose={() => setViewerFile(null)}
          onDownload={() => void handleDownload(viewerFile)}
        />
      )}
    </div>
  );
});
