/**
 * Utility for extracting files from drag-and-drop events, including
 * recursive traversal of dropped directories using the File and Directory
 * Entries API (webkitGetAsEntry).
 */

// File and Directory Entries API type declarations
// These are widely supported but not in TypeScript's default lib
interface FileSystemEntry {
  readonly isFile: boolean;
  readonly isDirectory: boolean;
  readonly name: string;
  readonly fullPath: string;
}

interface FileSystemFileEntry extends FileSystemEntry {
  file(successCallback: (file: File) => void, errorCallback?: (err: DOMException) => void): void;
}

interface FileSystemDirectoryEntry extends FileSystemEntry {
  createReader(): FileSystemDirectoryReader;
}

interface FileSystemDirectoryReader {
  readEntries(
    successCallback: (entries: FileSystemEntry[]) => void,
    errorCallback?: (err: DOMException) => void
  ): void;
}

/** Filenames to silently exclude from folder uploads (OS metadata files). */
const IGNORED_FILENAMES = new Set(['.DS_Store']);

/**
 * Represents a file with its relative path within a dropped folder structure.
 */
export interface FileWithPath {
  file: File;
  /** Relative path including parent directories, e.g. "test/sub/file.txt" */
  relativePath: string;
}

/**
 * Recursively traverse a FileSystemEntry to collect all files with their
 * relative paths.
 */
async function traverseEntry(
  entry: FileSystemEntry,
  pathPrefix: string
): Promise<FileWithPath[]> {
  if (entry.isFile) {
    const fileEntry = entry as FileSystemFileEntry;
    const file = await new Promise<File>((resolve, reject) => {
      fileEntry.file(resolve, reject);
    });
    return [{ file, relativePath: pathPrefix + entry.name }];
  }

  if (entry.isDirectory) {
    const dirEntry = entry as FileSystemDirectoryEntry;
    const reader = dirEntry.createReader();
    const results: FileWithPath[] = [];

    // readEntries returns batches; must loop until empty
    let batch: FileSystemEntry[];
    do {
      batch = await new Promise<FileSystemEntry[]>((resolve, reject) => {
        reader.readEntries(resolve, reject);
      });
      for (const child of batch) {
        const childFiles = await traverseEntry(
          child,
          pathPrefix + entry.name + '/'
        );
        results.push(...childFiles);
      }
    } while (batch.length > 0);

    return results;
  }

  return [];
}

/**
 * Given a DataTransfer from a drop event, extract all files including
 * those nested inside directories. Returns files with their relative paths.
 *
 * Uses the File and Directory Entries API (webkitGetAsEntry) to recursively
 * traverse dropped directories. Falls back to dataTransfer.files for
 * browsers that don't support the entries API.
 */
export async function extractFilesFromDataTransfer(
  dataTransfer: DataTransfer
): Promise<FileWithPath[]> {
  const results: FileWithPath[] = [];
  const items = dataTransfer.items;

  if (items && items.length > 0) {
    // Try using the entries API for directory support
    const entries: FileSystemEntry[] = [];
    let hasEntries = false;

    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      if (item.kind === 'file' && item.webkitGetAsEntry) {
        const entry = item.webkitGetAsEntry();
        if (entry) {
          entries.push(entry);
          hasEntries = true;
        }
      }
    }

    if (hasEntries) {
      // Traverse all entries (files and directories)
      for (const entry of entries) {
        const entryFiles = await traverseEntry(entry, '');
        results.push(...entryFiles);
      }
      return results.filter((f) => !IGNORED_FILENAMES.has(f.file.name));
    }
  }

  // Fallback: use dataTransfer.files (no directory support)
  const files = dataTransfer.files;
  if (files) {
    for (let i = 0; i < files.length; i++) {
      results.push({ file: files[i], relativePath: files[i].name });
    }
  }

  return results.filter((f) => !IGNORED_FILENAMES.has(f.file.name));
}
