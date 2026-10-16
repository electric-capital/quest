/**
 * UploadProgress -- the one-line "Uploading report.pdf (120 MB)… 42%" strip
 * shown above a composer while queued files are on their way to the server.
 *
 * Both composer hosts show it: HomeComposer while the first send creates
 * the conversation and uploads its files (the composer is cleared and
 * disabled meanwhile, so without this strip a large upload looks like a
 * dead screen), and ChatPanel while a later turn's files upload before the
 * send. The host owns the state (``describeUpload`` builds the label from
 * the queued File[]; the percent comes from ``uploadFiles``' XHR progress
 * callback) and renders this purely presentational component.
 */

import './UploadProgress.css';

export interface UploadStatus {
  /** What is being uploaded, e.g. "Uploading 3 files (140 MB)…". */
  label: string;
  /** 0-100 once the XHR has reported progress; null before the first event
   *  (or for uploads without progress events, such as pasted images). */
  percent: number | null;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

/** Label for a batch of queued generic files: one file is named, several are counted. */
export function describeUpload(files: File[]): string {
  const total = files.reduce((sum, f) => sum + f.size, 0);
  const size = formatBytes(total);
  if (files.length === 1) return `Uploading ${files[0].name} (${size})…`;
  return `Uploading ${files.length} files (${size})…`;
}

/** Label for pasted images, which upload without progress events. */
export function describeImageUpload(count: number): string {
  return count === 1 ? 'Uploading pasted image…' : `Uploading ${count} pasted images…`;
}

/** Percent for a progress callback, clamped so a 0-byte total never yields NaN. */
export function uploadPercent(loaded: number, total: number): number {
  if (total <= 0) return 100;
  return Math.min(100, Math.round((loaded / total) * 100));
}

interface UploadProgressProps {
  status: UploadStatus;
}

export function UploadProgress({ status }: UploadProgressProps) {
  const { label, percent } = status;
  return (
    <div className="upload-progress" role="status" aria-live="polite">
      <div className="upload-progress-row">
        <span className="upload-progress-spinner" aria-hidden="true" />
        <span className="upload-progress-label">{label}</span>
        {percent !== null && (
          <span className="upload-progress-percent">{percent}%</span>
        )}
      </div>
      <div
        className={`upload-progress-track${percent === null ? ' upload-progress-track-indeterminate' : ''}`}
      >
        <div
          className="upload-progress-fill"
          style={percent === null ? undefined : { width: `${percent}%` }}
        />
      </div>
    </div>
  );
}
