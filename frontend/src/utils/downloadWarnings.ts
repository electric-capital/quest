/**
 * Which workspace downloads get the hidden-data warning, and what it says.
 *
 * A prompt-injected agent can write files into the workspace that carry
 * information the user never sees: a script inside an HTML page, bytes in an
 * image's EXIF block, a macro or embedded object in an Office document, an
 * extra member in an archive. Nothing on this side inspects the bytes (a
 * sanitization layer is planned); this only decides whether a download of a
 * given name must first be acknowledged, and which explanation to show.
 *
 * The rule is allow-list shaped: a download is silent only when the extension
 * is a plain-text format whose whole content is visible in a text viewer.
 * Everything else -- including an unknown or missing extension, since the
 * agent picks the filename -- warns, with a category-specific explanation
 * where one applies and a generic one otherwise.
 */

export type DownloadWarningCategory =
  | 'web'
  | 'image'
  | 'document'
  | 'archive'
  | 'media'
  | 'executable'
  | 'other';

export interface DownloadWarning {
  category: DownloadWarningCategory;
  /** One sentence naming the hidden-data channel this file type has. */
  detail: string;
}

export interface DownloadTarget {
  /** Display name of what is downloaded: the file name, or the folder name for a zip. */
  name: string;
  /** A folder download is always an archive, whatever the folder is called. */
  kind: 'file' | 'folder';
}

/**
 * Plain-text formats that download without a warning: what the user can read
 * in a text viewer is the whole file. Source code is included on that basis,
 * except the browser-executed formats listed under `web` below.
 */
const PLAIN_TEXT_EXTENSIONS = new Set([
  'txt', 'md', 'markdown', 'rst', 'csv', 'tsv', 'log',
  'json', 'jsonl', 'ndjson', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf', 'env', 'xml',
  'sql', 'py', 'rb', 'go', 'rs', 'java', 'kt', 'swift', 'c', 'h', 'cpp', 'hpp', 'cc', 'cs',
  'php', 'r', 'scala', 'ts', 'tsx', 'jsx', 'css', 'sh', 'bash', 'zsh', 'tex', 'bib', 'diff', 'patch',
]);

const CATEGORY_EXTENSIONS: Record<Exclude<DownloadWarningCategory, 'other'>, readonly string[]> = {
  web: ['html', 'htm', 'xhtml', 'xht', 'mhtml', 'mht', 'svg', 'js', 'mjs', 'cjs', 'webarchive'],
  image: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'avif', 'tif', 'tiff', 'heic', 'heif', 'psd'],
  document: [
    'pdf', 'doc', 'docx', 'docm', 'dot', 'dotx', 'xls', 'xlsx', 'xlsm', 'xlsb', 'ppt', 'pptx', 'pptm',
    'odt', 'ods', 'odp', 'rtf', 'epub', 'pages', 'numbers', 'key',
  ],
  archive: ['zip', 'tar', 'gz', 'tgz', 'bz2', 'xz', '7z', 'rar', 'jar', 'war', 'iso', 'dmg'],
  media: ['mp3', 'wav', 'flac', 'ogg', 'm4a', 'aac', 'mp4', 'm4v', 'mov', 'avi', 'mkv', 'webm'],
  executable: [
    'exe', 'msi', 'dll', 'com', 'scr', 'bat', 'cmd', 'ps1', 'vbs', 'wsf', 'hta', 'lnk', 'url',
    'app', 'pkg', 'deb', 'rpm', 'apk', 'command', 'reg',
  ],
};

const CATEGORY_DETAILS: Record<DownloadWarningCategory, string> = {
  web: 'Web files can contain scripts that run as soon as the file is opened in a browser, and markup that is not shown on the rendered page.',
  image: 'Images can carry data in their metadata (such as EXIF fields) and in pixel patterns that are invisible when the picture is viewed.',
  document: 'Documents can carry hidden metadata, embedded objects, macros or text that does not appear on the page.',
  archive: 'Archives can contain files of any type, including ones that were never shown in the workspace listing.',
  media: 'Audio and video files can carry data in their metadata and in extra streams that never play.',
  executable: 'Executable files run code on your computer the moment they are opened.',
  other: 'This file type cannot be inspected here, so it may carry data that is not visible when the file is opened.',
};

/** Lower-cased extension without the dot, or '' when the name has none. */
function extensionOf(name: string): string {
  const base = name.split('/').pop() ?? name;
  const dot = base.lastIndexOf('.');
  // A leading dot (".env") is a hidden-file marker, not an extension.
  if (dot <= 0) return '';
  return base.substring(dot + 1).toLowerCase();
}

function categoryForExtension(ext: string): DownloadWarningCategory | null {
  if (PLAIN_TEXT_EXTENSIONS.has(ext)) return null;
  for (const [category, extensions] of Object.entries(CATEGORY_EXTENSIONS)) {
    if (extensions.includes(ext)) return category as DownloadWarningCategory;
  }
  return 'other';
}

/**
 * The warning a download of `target` must show before it starts, or null when
 * the file type needs none.
 */
export function getDownloadWarning(target: DownloadTarget): DownloadWarning | null {
  const category = target.kind === 'folder' ? 'archive' : categoryForExtension(extensionOf(target.name));
  if (category === null) return null;
  return { category, detail: CATEGORY_DETAILS[category] };
}
