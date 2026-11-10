/**
 * What a Quest Tasks composer (TaskComposer) hands its host on Send, and
 * the one helper that turns it into a run: `startTaskRunFromComposer` calls
 * `startTaskRun` with the composer's model, thinking level and skills, and
 * uploads the queued attachments in its `beforeSend`, once the run
 * conversation exists and before the message goes out:
 *
 * - generic files go to the run conversation's workspace (`uploadFiles`)
 *   and their names ride the send as `attached_filenames`; a partial failure
 *   carries on with a warning, a failed request aborts the start;
 * - pasted images go through the composer-attachments endpoint
 *   (`uploadComposerAttachments`) and their refs ride the send as
 *   `attachments`; when every image is rejected the start aborts, so the
 *   prompt never goes out without the pictures it talks about.
 *
 * An aborted upload rejects with the TaskRunStartError `start_failed`
 * carrying the upload's message (see services/taskRuns.ts); the run
 * conversation is spent then, and a retry starts a fresh one.
 */

import { uploadComposerAttachments, uploadFiles } from '../../api/fileApi';
import type { ComposerAttachmentRef } from '../../api/types';
import { startTaskRun, type StartedTaskRun } from '../../services/taskRuns';
import {
  describeImageUpload,
  describeUpload,
  uploadPercent,
  type UploadStatus,
} from '../UploadProgress';

export interface TaskComposerSubmission {
  /** The prompt, trimmed (may be empty when only images were queued). */
  text: string;
  /** The picked model (null = the task's default). */
  model: string | null;
  /** The thinking level picked with the model (null = the model's default). */
  thinkingEffort: string | null;
  /** Queued skills to load into the run. */
  skillIds: string[];
  /** Queued generic files, uploaded to the run conversation's workspace. */
  files: File[];
  /** Pasted images (the composer defers their upload to the host). */
  imageFiles: File[];
}

export interface TaskRunUploadCallbacks {
  /** The upload strip's state while files upload; null once they are done. */
  onUploadStatus?: (status: UploadStatus | null) => void;
  /** Some attachments failed to upload but the run goes ahead without them. */
  onWarning?: (message: string) => void;
}

/** The upload strip to show from the moment Send is pressed, or null without attachments. */
export function initialUploadStatus(submission: TaskComposerSubmission): UploadStatus | null {
  if (submission.files.length > 0) {
    return { label: describeUpload(submission.files), percent: null };
  }
  if (submission.imageFiles.length > 0) {
    return { label: describeImageUpload(submission.imageFiles.length), percent: null };
  }
  return null;
}

/**
 * Start a run of `taskId` from a composer submission. Resolves once the
 * server has stored the prompt; rejects with a TaskRunStartError only
 * (`taskRunStartErrorMessage` gives the sentence to show).
 */
export function startTaskRunFromComposer(
  taskId: string,
  submission: TaskComposerSubmission,
  callbacks: TaskRunUploadCallbacks = {},
): Promise<StartedTaskRun> {
  const { files, imageFiles } = submission;
  const { onUploadStatus, onWarning } = callbacks;
  const hasUploads = files.length > 0 || imageFiles.length > 0;

  const beforeSend = hasUploads
    ? async (conversationId: string) => {
        let attachedFilenames: string[] = [];
        if (files.length > 0) {
          const label = describeUpload(files);
          onUploadStatus?.({ label, percent: null });
          const resp = await uploadFiles(conversationId, files, '', (loaded, total) => {
            onUploadStatus?.({ label, percent: uploadPercent(loaded, total) });
          });
          attachedFilenames = resp.uploadedFiles.map((f) => f.name);
          if (resp.errors && resp.errors.length > 0) {
            onWarning?.(
              `Some files could not be uploaded: ${resp.errors.map((e) => e.message).join('; ')}`,
            );
          }
        }

        let attachments: ComposerAttachmentRef[] = [];
        if (imageFiles.length > 0) {
          onUploadStatus?.({ label: describeImageUpload(imageFiles.length), percent: null });
          const resp = await uploadComposerAttachments(conversationId, imageFiles);
          attachments = resp.attachments ?? [];
          if (resp.errors && resp.errors.length > 0) {
            const detail = resp.errors.map((e) => e.message).join('; ');
            if (attachments.length === 0) {
              throw new Error(`Pasted images could not be uploaded: ${detail}`);
            }
            onWarning?.(`Some pasted images could not be uploaded: ${detail}`);
          }
        }

        onUploadStatus?.(null);
        return { attachedFilenames, attachments };
      }
    : undefined;

  return startTaskRun(taskId, {
    message: submission.text,
    model: submission.model,
    thinkingEffort: submission.thinkingEffort,
    skillIds: submission.skillIds,
    beforeSend,
  });
}
