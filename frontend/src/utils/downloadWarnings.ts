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
 * is an inert text format -- one with no behavior attached to opening it,
 * whose whole content is visible in a text viewer (txt, json, yaml, ...).
 * Everything else -- including an unknown or missing extension, since the
 * agent picks the filename -- warns, with a category-specific explanation
 * where one applies and a generic one otherwise. Text is split three ways:
 * inert data is silent, markup that a viewer renders (and whose references
 * it fetches, e.g. a markdown image URL) warns, and source code is its own
 * severe category because the real hazard is running it.
 *
 * For the raster formats the server can rewrite (`SANITIZABLE_IMAGE_EXTENSIONS`,
 * see chat/image_sanitizer.py) the warning additionally offers a sanitized
 * copy -- pixels kept, every other part of the file dropped -- and the original
 * only behind a second, explicit acknowledgement.
 */

export type DownloadWarningCategory =
  | 'markup'
  | 'code'
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
  /**
   * `severe` marks the types that do not merely carry data but DO something
   * when used (code, executables): the dialog adds a do-not-run paragraph
   * and uses the danger tone.
   */
  severity: 'warning' | 'severe';
  /**
   * Set when the server offers a metadata-stripped copy of this file
   * (`GET .../files/download-sanitized`): the dialog then leads with
   * "Download Sanitized Copy" and gates the original behind a second confirm.
   */
  sanitizer?: 'image';
}

export interface DownloadTarget {
  /** Display name of what is downloaded: the file name, or the folder name for a zip. */
  name: string;
  /** A folder download is always an archive, whatever the folder is called. */
  kind: 'file' | 'folder';
}

/**
 * Inert text: formats with no behavior attached to opening them -- no
 * references another program resolves, nothing that runs. What the user can
 * read in a text viewer is the whole file, so these download silently.
 * CSV/TSV are NOT here: a spreadsheet application evaluates formula cells
 * on open (=HYPERLINK, =IMPORTXML, ...), which is the markup hazard.
 */
const INERT_TEXT_EXTENSIONS = new Set([
  'txt', 'log', 'json', 'jsonl', 'ndjson',
  'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf', 'env', 'bib',
]);

const CATEGORY_EXTENSIONS: Record<Exclude<DownloadWarningCategory, 'other'>, readonly string[]> = {
  // Text formats a viewer RENDERS, resolving references on the way: a
  // markdown image URL, a CSS url(), an XML stylesheet PI each fetch a
  // remote resource -- and the request itself can carry the leaked data.
  // A spreadsheet app evaluating a CSV's formula cells is the same hazard.
  markup: ['md', 'markdown', 'rst', 'xml', 'css', 'csv', 'tsv'],
  // Source that does something when run, imported, compiled or applied.
  code: [
    'py', 'rb', 'go', 'rs', 'java', 'kt', 'swift', 'c', 'h', 'cpp', 'hpp', 'cc', 'cs', 'php', 'r',
    'scala', 'ts', 'tsx', 'jsx', 'js', 'mjs', 'cjs', 'sh', 'bash', 'zsh', 'sql', 'ipynb', 'tex',
    'diff', 'patch',
  ],
  web: ['html', 'htm', 'xhtml', 'xht', 'mhtml', 'mht', 'svg', 'webarchive'],
  image: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'avif', 'tif', 'tiff', 'heic', 'heif', 'psd'],
  document: [
    'pdf', 'doc', 'docx', 'docm', 'dot', 'dotx', 'xls', 'xlsx', 'xlsm', 'xlsb', 'ppt', 'pptx', 'pptm',
    'odt', 'ods', 'odp', 'rtf', 'epub', 'pages', 'numbers', 'key',
  ],
  archive: ['zip', 'tar', 'gz', 'tgz', 'bz2', 'xz', '7z', 'rar', 'jar', 'war', 'iso', 'dmg'],
  media: ['mp3', 'wav', 'flac', 'ogg', 'm4a', 'aac', 'mp4', 'm4v', 'mov', 'avi', 'mkv', 'webm'],
  // Runs the moment the file is opened, no interpreter step in between.
  executable: [
    'exe', 'msi', 'dll', 'com', 'scr', 'bat', 'cmd', 'ps1', 'vbs', 'wsf', 'hta', 'lnk', 'url',
    'app', 'pkg', 'deb', 'rpm', 'apk', 'command', 'reg',
  ],
};

const CATEGORY_DETAILS: Record<DownloadWarningCategory, string> = {
  markup: 'Files like this are rendered by the program that opens them, and the rendering can fetch remote resources the file references -- a markdown image URL, or a formula cell a spreadsheet evaluates in a CSV -- so simply viewing the file can send information to a third party.',
  code: 'Source code can do anything when it runs: read or change files on your computer, connect to the network, send data elsewhere. Nothing here has inspected what it does.',
  web: 'Web files can contain scripts that run as soon as the file is opened in a browser, and markup that is not shown on the rendered page.',
  image: 'An image file can hold hidden information that you never see when you look at the picture.',
  document: 'Documents can carry hidden metadata, embedded objects, macros or text that does not appear on the page.',
  archive: 'Archives can contain files of any type -- code, web pages, images, documents -- none of which were inspected. Every file inside carries the same risks as if it were downloaded on its own.',
  media: 'Audio and video files can carry data in their metadata and in extra streams that never play.',
  executable: 'Executable files run code on your computer the moment they are opened.',
  other: 'This file type cannot be inspected here, so it may carry data that is not visible when the file is opened.',
};

const SEVERE_CATEGORIES: ReadonlySet<DownloadWarningCategory> = new Set(['code', 'executable']);

/**
 * The raster formats chat/image_sanitizer.py rewrites. Hand-mirrored: the
 * server picks the format by magic bytes and 400s anything else, so a file
 * named .png that is not a PNG fails the sanitized fetch rather than
 * silently downloading.
 */
export const SANITIZABLE_IMAGE_EXTENSIONS: ReadonlySet<string> = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp']);

/** What the sanitized copy is, for the dialog: plain words, one sentence. */
export const SANITIZED_IMAGE_EXPLANATION =
  'The sanitized copy is the same picture with everything hidden stripped out.';

/** Lower-cased extension without the dot, or '' when the name has none. */
function extensionOf(name: string): string {
  const base = name.split('/').pop() ?? name;
  const dot = base.lastIndexOf('.');
  // A leading dot (".env") is a hidden-file marker, not an extension.
  if (dot <= 0) return '';
  return base.substring(dot + 1).toLowerCase();
}

function categoryForExtension(ext: string): DownloadWarningCategory | null {
  if (INERT_TEXT_EXTENSIONS.has(ext)) return null;
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
  const ext = target.kind === 'folder' ? '' : extensionOf(target.name);
  const category = target.kind === 'folder' ? 'archive' : categoryForExtension(ext);
  if (category === null) return null;
  const warning: DownloadWarning = {
    category,
    detail: CATEGORY_DETAILS[category],
    severity: SEVERE_CATEGORIES.has(category) ? 'severe' : 'warning',
  };
  if (category === 'image' && SANITIZABLE_IMAGE_EXTENSIONS.has(ext)) warning.sanitizer = 'image';
  return warning;
}
