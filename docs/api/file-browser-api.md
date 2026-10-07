# File Browser API Documentation

This document describes the File Browser API endpoints for managing files within conversation workspaces and project workspaces.

## Overview

The File Browser API provides endpoints for listing, uploading, downloading, creating folders, and deleting files in a conversation's workspace directory. Each conversation has an isolated workspace folder that Gemini can access during tool execution.

Two workspaces are browsable (devplan 00009):

- the **conversation workspace** `data/chats/{id}/workspace/` (`ChatStorage.get_conversation_workspace_root`), for every conversation -- standalone or in a project -- under `/app/api/conversations/{conversation_id}/files`;
- the **project workspace** `data/projects/{id}/workspace/workspace/` (`ChatStorage.get_project_workspace_root`), shared by every conversation of a project, under `/app/api/projects/{project_id}/files`.

A path passed to one route set resolves only inside that set's workspace; neither falls back to the other space on a miss.

## Key Files

| File | Description |
|------|-------------|
| `chat/file_routes.py` | File browser endpoint implementations (list, upload, download, download folder as zip, read content, file info, delete, create folder, save to Drive) as root-parameterised handlers (`_list`, `_upload`, `_read_content`, `_download`, `_download_folder`, `_info`, `_delete`, `_create_folder`, `_save_to_drive`) behind the conversation and project route sets, plus the copy-to-project / copy-from-project routes |
| `chat/file_storage.py` | File operations, path validation, directory conflict detection, text content reading (`get_file_content()`), file/folder info (`count_workspace_item_files()`), deletion (`delete_workspace_item()`), folder zipping (`create_folder_zip()`), folder creation (`create_workspace_folder()`), copying between workspace roots (`copy_entry()`, `CopyEntryError`, `is_scratch_source()`), and upload size limit (`MAX_FILE_SIZE`, 200MB) |
| `quest.py` | Route registration |
| `frontend/src/components/FileBrowser.tsx` | UI component for file browsing (supports file and folder drag-and-drop, opens FileViewerModal for viewable text files, JSON files, images, PDFs, and CSVs, folder download as zip, split Upload Files + New Folder action buttons with Lucide icons, hides dot-prefixed entries by default behind a stateful Eye/EyeOff toggle -- client-side only); uses Lucide icons via `getFileIconInfo()` for extension-specific file icons and `Folder` for directories |
| `frontend/src/components/FileBrowser.css` | FileBrowser styling (includes multi-line error display, upload progress bar, zipping notification bar, equal-width action button row, show-hidden toggle button, hidden-count empty-state hint, and 16 icon color classes for file-type icons in dark/light mode) |
| `frontend/src/components/NewFolderModal.tsx` | Portal modal for creating a new folder in the currently-viewed workspace directory; mirrors the `NewProjectModal` pattern (auto-focus input, trim, disable while creating, inline error display) |
| `frontend/src/components/NewFolderModal.css` | NewFolderModal styling with dark/light mode variants |
| `frontend/src/components/FileViewerModal.tsx` | Full-screen modal for viewing text files, JSON files (via `JsonTreeViewer`), images, PDFs (via `PdfViewer`), and CSVs (sortable table) with download button and Save to Drive button (for `.md` files); images and PDFs fetched via the download endpoint with metadata display; PDFs over 100 MB (`PDF_MAX_PREVIEW_BYTES`) fall back to a download prompt; `.md` files render as HTML client-side (react-markdown, no backend change) with a "Show rendered" toggle to flip to the raw source; `.csv` files parse client-side and render as a sortable table with a "Show raw" toggle -- see [Frontend Architecture](../architecture/frontend.md#right-panel-file-browser-and-project-tables) |
| `frontend/src/utils/csv.ts` | Dependency-free RFC-4180-aware CSV parser (`parseCsv`) used by `FileViewerModal` for the CSV table preview; never throws (falls back to raw display on failure) |
| `frontend/src/components/FileViewerModal.css` | FileViewerModal styling (includes checkerboard background for image transparency) |
| `frontend/src/components/JsonTreeViewer.tsx` | Recursive collapsible tree viewer for JSON files with syntax coloring, expand/collapse toggles, and item count badges; dark/light mode support |
| `frontend/src/components/JsonTreeViewer.css` | JsonTreeViewer styling (syntax colors, toggle controls, dark/light mode variants) |
| `frontend/src/components/PdfViewer.tsx` | In-modal pdf.js PDF viewer (thumbnail rail + fit-to-width pages, lazy canvas rendering, self-hosted worker/assets); see [Frontend Architecture](../architecture/frontend.md#right-panel-file-browser-and-project-tables) |
| `frontend/src/hooks/useFileBrowser.ts` | State management hook for file browser (includes `uploadFilesWithPaths` for folder uploads, `uploadPercent` state for progress tracking, `deleteItem` for file/folder deletion, `downloadFile` / `downloadFolder` behind the hidden-data download warning, and `createFolder` for creating new folders in the current path) |
| `frontend/src/api/fileApi.ts` | Frontend API client functions (includes `xhrUpload()` helper for XHR-based uploads with progress callback, `uploadFiles()` and `uploadFilesWithPaths()` with `onProgress` parameter, `fetchFileContent()`, `getFileInfo()`, `deleteFile()`, `downloadFolder()`, `createFolder()`, `saveToDrive()`, and `saveBlobToDisk()` -- the temporary-anchor step every workspace download ends with) |
| `frontend/src/contexts/DownloadWarningContext.tsx`, `frontend/src/hooks/useWorkspaceDownload.ts`, `frontend/src/utils/downloadWarnings.ts` | The hidden-data download warning: every UI download of a non-plain-text file (and every folder zip) is acknowledged in a modal before the fetch -- see [Frontend Architecture -- Workspace Download Warning](../architecture/frontend.md#workspace-download-warning) |
| `frontend/src/utils/fileIcons.ts` | Extension-to-icon mapping utility; maps ~50 file extensions across 16 categories to Lucide icon components and CSS color classes via `getFileIconInfo()` |
| `frontend/src/utils/directoryTraversal.ts` | Recursive directory traversal for drag-and-drop folder uploads via `webkitGetAsEntry()` API |

## Authentication

All File Browser endpoints support dual authentication: session cookie OR API key Bearer token (same as other `/app/api/*` endpoints).

- **Browser frontend**: Uses the session cookie automatically (name from `COOKIE_NAME` in `auth/config.py`)
- **Scripts and LLM agents**: Use `Authorization: Bearer YOUR_API_KEY` header

**Implementation:** Uses the same `get_current_user` dependency as other chat endpoints (see `chat/auth.py`)

**Error codes:**
- `401` - Not authenticated (no valid cookie or API key)
- `403` - Access restricted (wrong email domain)
- `404` - Conversation (or project) not found or not owned by the caller

## Endpoints

All endpoints are defined in `chat/file_routes.py`. File operations (path validation, content reading, deletion) are in `chat/file_storage.py`. See those files for parameters, request/response shapes, and error codes.

The mutating routes (upload, composer attachments, delete, create-folder, and their project twins, plus the copy routes) publish a per-user `file_list_changed` global on the realtime bus after a successful write so file browsers in any of the user's other open tabs silent-refresh.

The publish is a best-effort `bus.publish_to_user(...)` wrapped in try/except via `_publish_file_list_changed` in `chat/file_routes.py`; a publish failure never rolls back the underlying write.

Each route passes `scope` explicitly: the conversation routes publish `scope="conversation"` (project conversations included, with their `project_id` still on the envelope), the project routes `scope="project"` with `conversation_id: null` (so every tab of the project refreshes), and the copy routes one event per scope. See [Realtime Architecture](../architecture/realtime.md#backend-publish-sites-per-user-globals) for the publish sites and [Subscription Protocol](../architecture/realtime.md#subscription-protocol) for the wire envelope.

Endpoints under `/app/api/conversations/{conversation_id}/files`:

- **GET `/files`** -- List files/folders at a path (`list_files()`). Returns **every** entry, including dot-prefixed ones (`.responses/`, `.temp/`, etc.); the API does not filter hidden files. The frontend `FileBrowser` hides dot-prefixed entries from the rendered list by default (revealable via an Eye/EyeOff toggle), but that is purely client-side -- see [Frontend Architecture](../architecture/frontend.md#right-panel-file-browser-and-project-tables)
- **POST `/files/upload`** -- Upload files or folders (`upload_files()`). Maximum file size is 200MB (`MAX_FILE_SIZE` in `chat/file_storage.py`). Supports folder uploads via a parallel `paths` form parameter paired with `files` by index.
  - The frontend uses XMLHttpRequest instead of fetch() for uploads to provide real-time upload progress tracking (see `xhrUpload()` in `frontend/src/api/fileApi.ts`).
  - This is also the route the composer "Attach" button uses to upload generic pre-send file attachments (any extension) into the conversation workspace before the first/next `send_message` -- no new endpoint was added for that feature; the uploaded filenames then ride the `send_message` frame as `attached_filenames`. See [Frontend -- Composer Component](../architecture/frontend.md#composer-component) and [Chat API -- Send Message](chat-api.md#send-message).
- **GET `/files/content`** -- Read text content for inline viewing (`read_file_content()`). Viewable extensions and size limits are defined in `chat/file_storage.py` (`VIEWABLE_EXTENSIONS`, `MAX_VIEW_SIZE`). Markdown (`.md`) and CSV (`.csv`) files are served as raw text here too; the HTML rendering for `.md` (with the "Show rendered" toggle) and the sortable-table rendering for `.csv` (client-side parse via `frontend/src/utils/csv.ts`, with the "Show raw" toggle) both happen in `FileViewerModal` -- no preview-specific backend endpoint exists
- **GET `/files/download`** -- Download a file (`download_file()`). On the frontend every user-facing download of this route (and of `/files/download-folder`) first passes the hidden-data warning dialog unless the file type is plain text (`frontend/src/utils/downloadWarnings.ts`); the route itself is unchanged and does not enforce it -- see [Frontend Architecture -- Workspace Download Warning](../architecture/frontend.md#workspace-download-warning). Also the fetch path for inline image and PDF preview in `FileViewerModal`, and the `src` target for inline markdown workspace images in chat (cookie auth; no preview-specific backend endpoint exists). Raster image extensions (`_INLINE_IMAGE_MIMES` in `chat/file_routes.py`: png/jpg/jpeg/gif/webp) are served with their real MIME type and `Content-Disposition: inline` so `<img>` embeds and direct opens work; SVG is deliberately excluded (inline `image/svg+xml` would execute scripts on the app origin) and everything else remains an `application/octet-stream` attachment
- **GET `/files/info`** -- Get file/folder metadata including recursive file count (`get_file_info()`)
- **DELETE `/files`** -- Delete a file or folder recursively (`delete_file()`)
- **POST `/files/create-folder`** -- Create a new empty folder inside the workspace under the given parent path (`create_folder()`). Backed by `create_workspace_folder()` in `chat/file_storage.py`, which reuses `validate_path()`, `_sanitize_filename()`, and `_check_dir_conflicts()` for the same path-traversal, Unicode-space, and ancestor-file-conflict protections as the upload path. The folder name is rejected if empty, if it contains `/`, `\\`, or `..`, or if it collides with an existing entry.

### Project workspace routes

`/app/api/projects/{project_id}/files`, `/files/upload`, `/files/content`, `/files/download`, `/files/download-folder`, `/files/info`, `DELETE /files`, `/files/create-folder`, `/files/save-to-drive` mirror the conversation routes above with the same handlers and error shapes, over `resolve_owned_project_workspace()` in `chat/conversation_access.py` (404 `not_found` unless the caller owns the project). The project root is created on first access, so listing a project that never wrote a file returns an empty list. There is no project composer-attachment route: `pasted/` is conversation scratch.

### Copy / move routes

`POST /app/api/conversations/{conversation_id}/files/copy-to-project` and `/files/copy-from-project` copy (or, with `move: true`, move) a file or folder between a project conversation's workspace and its project's workspace. Body: `{path, dest?, overwrite?, move?, include_hidden?}` (`CopyEntryRequest`; `dest` defaults to `path`). The work is `copy_entry()` in `chat/file_storage.py`, run via `asyncio.to_thread`; see its docstring for the rules (both ends validated with `validate_path`, symlink / special-file leaves refused, symlinks and special files inside a folder skipped, dot-entries skipped unless named or `include_hidden`, folder merge on `overwrite`, a destination that is the source, inside it or (same root) an ancestor of it refused, every conflict found before anything is written, mid-copy symlink swaps refused, a failed copy into a fresh destination removed again incl. the parents it created, permission bits masked to `0o777`, folder nesting capped at `MAX_COPY_DEPTH` (256), a move deletes only what was copied). Errors use the usual `{error, message}` detail:

- 400 `not_a_project_conversation` -- the conversation is standalone;
- 400 `forbidden_source` -- copy-to-project from the conversation scratch roots `.responses/`, `.subagent_responses/`, `pasted/` (`is_scratch_source()`); copy-from-project does not refuse them, since copying into the conversation workspace promotes nothing;
- 400 `invalid_path` / `invalid_destination` / `not_a_regular_file`, 404 `not_found` (missing source), 409 `destination_exists` (without `overwrite`, or a destination that appeared concurrently);
- 500 `copy_failed` -- an I/O error while copying (e.g. disk full); both `file_list_changed` scopes are still published since part of a merge may have been written.

A move whose copy completed but whose source removal failed is not an error: the route returns 200 with `moved: false` (the failure is logged) and the source stays in place.

A separate composer-attachment upload route, `POST /app/api/conversations/{id}/composer-attachments`, persists clipboard-pasted images (PNG/JPEG only) into `workspace/pasted/<attachment_id>.<ext>` ahead of the next `send_message` WS frame. It is documented under [Chat API -- Composer Attachments](chat-api.md#composer-attachments) rather than here because the lifecycle is tied to the composer/send path, not the generic file browser.

All paths are validated to prevent directory traversal attacks. Paths must resolve within the workspace directory.

**Symlink policy:** symlinks are banned from workspaces entirely. The only place one could ever be created is the script sandbox's read-write workspace mount, and its seccomp profile denies the `symlink`/`symlinkat` syscalls (see [Script Runner -- Security](../architecture/script-runner.md#security)); a startup sweep (`chat/workspace_symlinks.py`, called from the quest.py lifespan) deletes any link that predates that fix.

The file endpoints keep defense-in-depth guards anyway:

- uploads open the destination leaf with `O_NOFOLLOW` (`_no_follow_opener` in `chat/file_storage.py`, so an existing symlink can never redirect the truncating write outside the workspace)
- folder zip downloads refuse to archive a folder containing a symlink at any depth (`create_folder_zip`)
- workspace duplication skips symlinks (and special files) instead of dereferencing them (`ChatStorage.copy_workspace_files(src_root, dst_root)` in `chat/storage.py`).

**Off-thread filesystem work:** the list, preview, folder-zip, info, and delete routes run their sync `chat/file_storage.py` helpers via `asyncio.to_thread`. A sandbox script can plant an arbitrarily large or deep tree in its workspace, and the info/delete `rglob` counts and the zip walk are unbounded, so calling them inline from the `async` handler stalled every other request and WebSocket on the server for the whole walk (finding #279201). The helpers themselves stay synchronous and are unit-tested as such; tests/test_file_routes_offload.py checks each route keeps the loop responsive.

**Non-regular files:** the sandbox can still create FIFOs (and other special files) in the workspace mount, and a blocking `open()` on a writer-less FIFO never returns -- fatal for the preview and Save-to-Drive routes, which would hang a worker thread forever (and, before the sync helpers were offloaded, the whole event loop). `get_file_content` therefore requires a regular file: an `is_file()` pre-check, then a non-blocking `os.open` with an `S_ISREG` re-check on the descriptor before reading (so a file swapped for a FIFO between check and open is still rejected). `create_folder_zip` likewise refuses a folder containing any non-regular descendant. Both surface as 400 `invalid_path`.

## Design Decisions

**Why sanitize Unicode spaces in filenames?**
macOS screenshot filenames contain narrow no-break spaces (U+202F) that are visually indistinguishable from regular ASCII spaces. When a user uploads a screenshot and asks Gemini to analyze it, the model reproduces the filename with regular spaces, causing a "file not found" error. The `_sanitize_filename()` function in `chat/file_storage.py` normalizes all Unicode space variants to ASCII spaces at upload time using `_UNICODE_SPACES_RE`, which matches characters in Unicode category Zs (U+00A0, U+1680, U+2000-U+200A, U+202F, U+205F, U+3000). This ensures filenames are reproducible by LLMs and other tools.

**Why a parallel `paths` form parameter instead of using the file's `name` attribute?**
The HTML `File` object's `name` property only contains the leaf filename, not the directory structure. When a user drops a folder, the browser provides individual `File` objects with no path information. The `webkitGetAsEntry()` API (in `frontend/src/utils/directoryTraversal.ts`) can traverse directories and reconstruct relative paths, but these paths must be sent separately via the `paths` form data array since `FormData` file entries cannot carry custom metadata. The backend pairs each file with its corresponding path by index.

**Why filter `.DS_Store` files?**
macOS creates hidden `.DS_Store` metadata files in every directory. These are never useful in a workspace and would clutter the file browser. The `IGNORED_FILENAMES` set in `frontend/src/utils/directoryTraversal.ts` filters them out during directory traversal before upload.

**Why detect directory conflicts with `_check_dir_conflicts()`?**
When uploading a folder named "test" but a file called "test" already exists in the workspace, Python's `Path.mkdir(parents=True)` raises an opaque `NotADirectoryError`. The `_check_dir_conflicts()` helper in `chat/file_storage.py` walks up from the target directory to the workspace root checking for this condition, and raises a descriptive `ValueError` instead.

**Why render CSV as a table client-side instead of adding a backend query layer?**
A CSV is just bytes on disk with no backing database, unlike project tables (which sort server-side via SQLite `ORDER BY`; see [Project DB API](project-db-api.md)). Adding `.csv` to `VIEWABLE_EXTENSIONS` lets the existing `/files/content` endpoint serve it as raw text (subject to the 1 MB `MAX_VIEW_SIZE` cap), and `FileViewerModal` parses and sorts it in the browser via `frontend/src/utils/csv.ts` -- no new endpoint or query plumbing. The viewer caps the table at 5000 rows and offers Download for the full file. See [Frontend Architecture -- Design Decisions (CSV Preview)](../architecture/frontend.md#right-panel-file-browser-and-project-tables).

**Why do images and PDFs use the download endpoint instead of a new content endpoint?**
The existing download endpoint already serves files as-is with correct content types. Fetching images as blobs (and PDFs as `ArrayBuffer`s for pdf.js) via this endpoint provides both the bytes for rendering and accurate file size. No backend changes were needed for either preview type. The frontend caps PDF preview at 100 MB (`PDF_MAX_PREVIEW_BYTES` in `frontend/src/components/FileViewerModal.tsx`) because the whole file is held in memory before the pdf.js handoff; oversize files get an error pointing at Download.

**Why use a temporary file and BackgroundTask for folder zip downloads?**
Zip archives can be large and must be fully constructed before streaming begins (the zip format requires a central directory at the end). Creating the zip as a temporary file and returning it as a `FileResponse` lets FastAPI handle the streaming efficiently. The `BackgroundTask` ensures the temp file is cleaned up after the response completes, avoiding disk accumulation.

**Why XMLHttpRequest instead of fetch() for uploads?**
The Fetch API does not support upload progress events. XMLHttpRequest's `upload.onprogress` provides `loaded` and `total` byte counts, enabling a real-time percentage display and progress bar during file uploads. The `xhrUpload()` helper in `frontend/src/api/fileApi.ts` wraps this with cookie-based auth and returns a Promise for consistent async handling.

**Why per-conversation file storage?**
Each conversation has its own isolated workspace directory (`data/chats/{id}/workspace/`), project conversations included. This ensures files from one conversation don't leak to another and matches the sandbox's workspace mount; sharing with sibling conversations is an explicit copy into the project workspace.

**Why path validation?**
All path parameters are validated to prevent directory traversal attacks (e.g., `../../../etc/passwd`). Paths must resolve to locations within the workspace directory.

**Why silent refresh for auto-refresh?**
When the model writes to the workspace (or another tab uploads / deletes / creates a folder), the file browser auto-refreshes using a "silent refresh" mode that fetches new data without showing a loading spinner.

The trigger is the `file_list_changed` per-user global on the realtime bus, emitted from each workspace-mutating tool handler and REST route on the success path; the FE filters by active conversation / project and debounces 200ms before calling `silentRefresh()` in `frontend/src/hooks/useFileBrowser.ts`.

The silent reload implements a stale-while-revalidate pattern: the UI continues displaying the existing file list while the new data loads in the background. This refreshes mid-turn after each successful write, decoupled from the streaming lifecycle. See [Realtime Architecture](../architecture/realtime.md) for the event taxonomy.

**Why refs to break circular dependencies?**
The `useFileBrowser` hook uses refs (`inFlightRef`, `conversationIdRef`, `browserStateRef`) to prevent infinite refresh loops that can occur when state updates trigger effects that update the same state, and to discard out-of-order responses. `inFlightRef` holds the `{ conversationId, path }` of the in-flight request and only suppresses a duplicate fetch when it matches the *same* conversation + path; `conversationIdRef` and `browserStateRef` let the async response handler compare against the current conversation/path and drop stale results without adding state to effect dependencies.

**Why a per-conversation in-flight guard instead of a shared boolean?**
The file browser previously used a single `isFetchingRef` boolean as the in-flight guard. When the user switched conversations, an in-flight fetch for the old conversation suppressed the first fetch for the new one, so the panel kept showing the previous conversation's files.

The guard is now keyed on `{ conversationId, path }` so a switch is never swallowed, and on a switch the hook resets the stale `files`/`canGoUp` state and refetches silently. Each fetch also captures its conversation + path and discards its result if the active context has moved on, so rapid A->B->A switching can't clobber the current list with a stale response.

The conversation-switch and folder-navigation effects are consolidated into one effect to avoid a same-commit race between them. See `frontend/src/hooks/useFileBrowser.ts`.

**Why per-conversation path state?**
Each conversation remembers its current browsing path separately. When switching between conversations, users return to where they left off in each workspace's file tree.

**Why `isInitialLoading` for button states?**
The component distinguishes between initial loading (no files yet) and background refreshes. Buttons are only disabled during initial load, not during stale-while-revalidate refreshes. This prevents buttons from flickering disabled/enabled during auto-refresh cycles.

