"""File storage utilities for workspace file browser."""

import errno
import logging
import os
import re
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple
import aiofiles

logger = logging.getLogger(__name__)

# Maximum file size for uploads (200MB)
MAX_FILE_SIZE = 200 * 1024 * 1024

# Regex matching any Unicode space character (category Zs) that is NOT a
# regular ASCII space (U+0020).  These look identical to normal spaces when
# rendered but cause mismatches when an LLM reproduces the filename.
# Covers: NO-BREAK SPACE, EN/EM SPACE, THIN SPACE, NARROW NO-BREAK SPACE, etc.
_UNICODE_SPACES_RE = re.compile(
    r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]"
)


def _sanitize_filename(filename: str) -> str:
    """Normalize a filename for safe, predictable storage.

    Replaces all Unicode space variants (narrow no-break space, en space,
    em space, etc.) with regular ASCII spaces so that filenames are
    reproducible by LLMs and other tools that cannot distinguish between
    visually-identical whitespace characters.
    """
    return _UNICODE_SPACES_RE.sub(" ", filename)


def _check_dir_conflicts(target_dir: Path, stop_at: Path) -> None:
    """Check that no ancestor of *target_dir* (down to *stop_at*) is a regular file.

    When a user uploads a folder named "test" but a *file* called "test"
    already exists in the workspace, ``Path.mkdir(parents=True)`` would
    raise an opaque ``NotADirectoryError``.  This helper detects the
    conflict early and raises a descriptive ``ValueError``.

    Args:
        target_dir: The directory that will be created.
        stop_at: The workspace root -- don't check above this.

    Raises:
        ValueError: If any path component is an existing regular file.
    """
    # Collect the ancestors from target_dir up to (but not including) stop_at
    parts_to_check: list[Path] = []
    current = target_dir
    resolved_stop = stop_at.resolve()
    while True:
        try:
            current.resolve().relative_to(resolved_stop)
        except ValueError:
            break
        if current.resolve() == resolved_stop:
            break
        parts_to_check.append(current)
        current = current.parent

    # Check from shallowest to deepest so the error names the first conflict
    for p in reversed(parts_to_check):
        if p.exists() and not p.is_dir():
            raise ValueError(
                f"Cannot create folder '{p.name}': a file with that name already exists"
            )


def validate_path(root: Path, requested_path: str) -> Tuple[bool, Optional[Path]]:
    """Validate that a requested path is within the workspace and safe.

    Prevents path traversal attacks by ensuring the resolved path
    is within ``root``. ``root`` is the browsable root itself (a
    conversation or project workspace root from ``ChatStorage``); nothing
    is appended to it. It is created if missing.

    Args:
        root: Browsable workspace root (the directory the user sees as "/")
        requested_path: User-requested relative path

    Returns:
        Tuple of (is_valid, resolved_path) where resolved_path is None if invalid
    """
    root.mkdir(parents=True, exist_ok=True)

    # Normalize and resolve the requested path
    if not requested_path or requested_path == "/" or requested_path == ".":
        return True, root

    # Clean the requested path - remove leading slashes
    clean_path = requested_path.lstrip("/").lstrip("\\")

    # Check for path traversal attempts
    if ".." in clean_path:
        return False, None

    # Resolve the full path
    full_path = (root / clean_path).resolve()

    # Ensure the resolved path is within the workspace
    try:
        full_path.relative_to(root.resolve())
        return True, full_path
    except ValueError:
        return False, None


def get_file_info(file_path: Path) -> Dict:
    """Get metadata for a file or directory.

    Args:
        file_path: Path to the file or directory

    Returns:
        Dictionary with file metadata
    """
    stat = file_path.stat()

    return {
        "name": file_path.name,
        "type": "folder" if file_path.is_dir() else "file",
        "size": stat.st_size if file_path.is_file() else None,
        "lastModified": datetime.fromtimestamp(stat.st_mtime).isoformat() + "Z"
    }


def list_workspace_files(root: Path, relative_path: str = "") -> Dict:
    """List contents of a directory within the workspace.

    Args:
        root: Browsable workspace root (the directory the user sees as "/")
        relative_path: Relative path within workspace (empty for root)

    Returns:
        Dictionary with currentPath, files list, and canGoUp boolean

    Raises:
        ValueError: If path validation fails
        FileNotFoundError: If directory doesn't exist
    """
    is_valid, resolved_path = validate_path(root, relative_path)

    if not is_valid or resolved_path is None:
        raise ValueError("Invalid path")

    if not resolved_path.exists():
        raise FileNotFoundError(f"Directory not found: {relative_path}")

    if not resolved_path.is_dir():
        raise ValueError(f"Not a directory: {relative_path}")

    # Calculate relative path for display
    try:
        current_path = "/" + str(resolved_path.relative_to(root))
        if current_path == "/.":
            current_path = "/"
    except ValueError:
        current_path = "/"

    # Determine if we can go up
    can_go_up = resolved_path.resolve() != root.resolve()

    # List directory contents
    files = []
    try:
        for entry in sorted(resolved_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                files.append(get_file_info(entry))
            except (PermissionError, OSError):
                # Skip files we can't access
                continue
    except PermissionError:
        raise ValueError("Permission denied")

    return {
        "currentPath": current_path,
        "files": files,
        "canGoUp": can_go_up
    }


def _no_follow_opener(path: str, flags: int) -> int:
    """Opener that refuses to write through a symlink leaf.

    Upload destinations are validated at the directory level only; the leaf
    is opened for truncating write afterwards. O_NOFOLLOW makes that open
    fail with ELOOP if the leaf is a symlink, so an existing link can never
    redirect the write outside the workspace (symlinks are banned from
    workspaces -- see chat/workspace_symlinks.py -- but the open itself must
    not follow one either; unlike a lstat-then-open check this is atomic).
    """
    return os.open(path, flags | os.O_NOFOLLOW)


async def save_uploaded_file(root: Path, filename: str, file_content: bytes, relative_path: str = "") -> Dict:
    """Save an uploaded file to the workspace.

    Args:
        root: Browsable workspace root (the directory the user sees as "/")
        filename: Name of the file to save
        file_content: File content as bytes
        relative_path: Relative path within workspace for upload destination

    Returns:
        Dictionary with file info

    Raises:
        ValueError: If path validation fails or file is too large
    """
    # Validate the filename
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError("Invalid filename")

    # Normalize unicode whitespace in filename (e.g. macOS narrow no-break
    # spaces in screenshot names) so the stored name uses only ASCII spaces.
    filename = _sanitize_filename(filename)

    # Check file size
    if len(file_content) > MAX_FILE_SIZE:
        raise ValueError(f"File too large. Maximum size is {MAX_FILE_SIZE // (1024 * 1024)}MB")

    # Validate destination path
    is_valid, resolved_dir = validate_path(root, relative_path)

    if not is_valid or resolved_dir is None:
        raise ValueError("Invalid destination path")

    # Ensure directory exists
    resolved_dir.mkdir(parents=True, exist_ok=True)

    # Save the file (never through a symlink leaf)
    file_path = resolved_dir / filename

    try:
        async with aiofiles.open(file_path, 'wb', opener=_no_follow_opener) as f:
            await f.write(file_content)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ValueError(
                f"Cannot overwrite '{filename}': it is a symbolic link"
            )
        raise


    # Return file info
    try:
        rel_path = "/" + str(file_path.relative_to(root))
    except ValueError:
        rel_path = "/" + filename

    return {
        "name": filename,
        "size": len(file_content),
        "path": rel_path
    }


def create_workspace_folder(
    root: Path,
    parent_relative_path: str,
    folder_name: str,
) -> Dict:
    """Create a new empty folder inside the workspace.

    Args:
        root: Browsable workspace root (the directory the user sees as "/")
        parent_relative_path: Relative path of the parent directory within
            the workspace (empty/"/" for root)
        folder_name: Name of the folder to create

    Returns:
        Dictionary with the created folder's name and full relative path.

    Raises:
        ValueError: If path or name validation fails, if the target already
            exists, or if an ancestor is a regular file.
        FileNotFoundError: If the parent path does not exist.
    """
    # Validate the folder name -- must be a non-empty string with no path
    # separators or traversal segments.  Mirrors the rules used by
    # save_uploaded_file() above.
    if not isinstance(folder_name, str):
        raise ValueError("Invalid folder name")

    # Normalize Unicode whitespace (e.g. NBSP) to ASCII space first so that
    # leading/trailing trims and length checks behave the same as for files.
    folder_name = _sanitize_filename(folder_name).strip()

    if not folder_name:
        raise ValueError("Folder name cannot be empty")
    if "/" in folder_name or "\\" in folder_name or ".." in folder_name:
        raise ValueError("Invalid folder name")
    if folder_name in (".", ".."):
        raise ValueError("Invalid folder name")
    # Cap to a reasonable filesystem-friendly length
    if len(folder_name) > 255:
        raise ValueError("Folder name is too long (max 255 characters)")

    # Resolve the parent directory inside the workspace
    is_valid, resolved_parent = validate_path(root, parent_relative_path)
    if not is_valid or resolved_parent is None:
        raise ValueError("Invalid path")

    if not resolved_parent.exists():
        raise FileNotFoundError(f"Parent directory not found: {parent_relative_path}")
    if not resolved_parent.is_dir():
        raise ValueError(f"Not a directory: {parent_relative_path}")

    target = resolved_parent / folder_name

    # Defense in depth: detect file-at-ancestor-path conflicts.  This is
    # mostly unreachable for a single-level mkdir (since the parent is
    # already validated as a directory) but matches the helper used by
    # save_uploaded_file_with_path().
    _check_dir_conflicts(target, root)

    if target.exists():
        raise ValueError(
            f"A file or folder named '{folder_name}' already exists"
        )

    # Create only the leaf directory.  parents=False ensures we never
    # silently materialize an unexpected ancestor chain.
    target.mkdir(parents=False, exist_ok=False)

    try:
        rel_path = "/" + str(target.relative_to(root))
    except ValueError:
        rel_path = "/" + folder_name

    return {
        "name": folder_name,
        "path": rel_path,
    }


async def save_uploaded_file_with_path(
    root: Path,
    relative_file_path: str,
    file_content: bytes,
    base_relative_path: str = ""
) -> Dict:
    """Save an uploaded file to a specific relative path within the workspace.

    This is used for folder uploads where each file has a relative path
    like "folder/subfolder/file.txt" that must be preserved.

    Args:
        root: Browsable workspace root (the directory the user sees as "/")
        relative_file_path: Relative path of the file including parent dirs
                           (e.g., "test/sub/file.txt")
        file_content: File content as bytes
        base_relative_path: Base destination path within workspace

    Returns:
        Dictionary with file info

    Raises:
        ValueError: If path validation fails or file is too large
    """
    # Validate relative_file_path - must not contain ".." components
    path_parts = relative_file_path.replace("\\", "/").split("/")
    if any(part == ".." for part in path_parts) or not path_parts:
        raise ValueError("Invalid file path")

    # Sanitize each path component
    sanitized_parts = [_sanitize_filename(part) for part in path_parts]
    # Filter out empty parts (from leading/trailing slashes)
    sanitized_parts = [p for p in sanitized_parts if p]
    if not sanitized_parts:
        raise ValueError("Invalid file path")

    filename = sanitized_parts[-1]
    # Validate the filename component (the leaf)
    if not filename:
        raise ValueError("Invalid filename")

    # Build the subdirectory path from the relative path's directory components
    subdir_parts = sanitized_parts[:-1]
    if subdir_parts:
        # Combine base path with the relative directory structure
        if base_relative_path and base_relative_path != "/":
            combined_path = base_relative_path.strip("/") + "/" + "/".join(subdir_parts)
        else:
            combined_path = "/".join(subdir_parts)
    else:
        combined_path = base_relative_path

    # Check file size
    if len(file_content) > MAX_FILE_SIZE:
        raise ValueError(
            f"File too large. Maximum size is {MAX_FILE_SIZE // (1024 * 1024)}MB"
        )

    # Validate destination path
    is_valid, resolved_dir = validate_path(root, combined_path)
    if not is_valid or resolved_dir is None:
        raise ValueError("Invalid destination path")

    # Check for file-at-directory-path conflicts before creating directories.
    # Walk up from the target dir toward the workspace root; if any component
    # already exists as a regular file, mkdir() would fail with an opaque
    # OSError / NotADirectoryError.  Raise a clear ValueError instead.
    _check_dir_conflicts(resolved_dir, root)

    # Ensure directory exists (creates nested dirs as needed)
    resolved_dir.mkdir(parents=True, exist_ok=True)

    # Save the file (never through a symlink leaf)
    file_path = resolved_dir / filename

    try:
        async with aiofiles.open(file_path, 'wb', opener=_no_follow_opener) as f:
            await f.write(file_content)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ValueError(
                f"Cannot overwrite '{filename}': it is a symbolic link"
            )
        raise


    # Return file info with full relative path
    try:
        rel_path = "/" + str(file_path.relative_to(root))
    except ValueError:
        rel_path = "/" + relative_file_path

    return {
        "name": filename,
        "size": len(file_content),
        "path": rel_path
    }


# Maximum file size for viewing (5MB)
MAX_VIEW_SIZE = 5 * 1024 * 1024

# File extensions supported for text viewing.
# `.csv` is served as raw text here; the frontend FileViewerModal parses it
# client-side into a styled, sortable table (with a "Show raw" toggle).
VIEWABLE_EXTENSIONS = {'.md', '.py', '.txt', '.json', '.csv'}


def get_file_content(root: Path, file_path_str: str) -> Tuple[str, str, int]:
    """Read text content of a file for viewing.

    Args:
        root: Browsable workspace root (the directory the user sees as "/")
        file_path_str: Relative path to the file

    Returns:
        Tuple of (content, filename, size)

    Raises:
        ValueError: If path validation fails, file type unsupported, or file too large
        FileNotFoundError: If file doesn't exist
    """
    is_valid, resolved_path = validate_path(root, file_path_str)

    if not is_valid or resolved_path is None:
        raise ValueError("Invalid path")

    if not resolved_path.exists():
        raise FileNotFoundError(f"File not found: {file_path_str}")

    if not resolved_path.is_file():
        raise ValueError(f"Not a file: {file_path_str}")

    # Check extension
    ext = resolved_path.suffix.lower()
    if ext not in VIEWABLE_EXTENSIONS:
        raise ValueError("File type not supported for viewing")

    # Only regular files are readable here. The sandbox can plant a FIFO (or
    # another special file) in the host-backed workspace; a blocking open()
    # on a writer-less FIFO would stall the request -- and the event loop --
    # forever. Open non-blocking and re-check the type on the descriptor so
    # a swap between the is_file() check and the open() can't slip through.
    fd = os.open(resolved_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"Not a file: {file_path_str}")

        # Check size
        size = st.st_size
        if size > MAX_VIEW_SIZE:
            raise ValueError(
                f"This file is too large to preview ({size / (1024 * 1024):.1f} MB). "
                f"The inline preview limit is {MAX_VIEW_SIZE // (1024 * 1024)} MB "
                f"— use Download to get the full file."
            )

        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as f:
            fd = -1  # ownership passed to the file object
            content = f.read()
    finally:
        if fd >= 0:
            os.close(fd)

    return content, resolved_path.name, size


def get_file_download(root: Path, file_path_str: str) -> Tuple[Path, str]:
    """Get a file path for download.

    Args:
        root: Browsable workspace root (the directory the user sees as "/")
        file_path_str: Relative path to the file

    Returns:
        Tuple of (file_path, filename)

    Raises:
        ValueError: If path validation fails
        FileNotFoundError: If file doesn't exist
    """
    is_valid, resolved_path = validate_path(root, file_path_str)

    if not is_valid or resolved_path is None:
        raise ValueError("Invalid path")

    if not resolved_path.exists():
        raise FileNotFoundError(f"File not found: {file_path_str}")

    if not resolved_path.is_file():
        raise ValueError(f"Not a file: {file_path_str}")

    return resolved_path, resolved_path.name


def create_folder_zip(root: Path, folder_path_str: str) -> Tuple[Path, str]:
    """Create a temporary zip archive of a folder within the workspace.

    Args:
        root: Browsable workspace root (the directory the user sees as "/")
        folder_path_str: Relative path to the folder within workspace

    Returns:
        Tuple of (temp_zip_path, folder_name)

    Raises:
        ValueError: If path validation fails or path is not a directory
        FileNotFoundError: If folder doesn't exist
    """
    is_valid, resolved_path = validate_path(root, folder_path_str)

    if not is_valid or resolved_path is None:
        raise ValueError("Invalid path")

    if not resolved_path.exists():
        raise FileNotFoundError(f"Folder not found: {folder_path_str}")

    if not resolved_path.is_dir():
        raise ValueError(f"Not a directory: {folder_path_str}")

    # Prevent downloading the entire workspace root as a zip
    if resolved_path.resolve() == root.resolve():
        raise ValueError("Cannot download the entire workspace as a zip")

    folder_name = resolved_path.name
    tmp_file = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
    temp_zip_path = Path(tmp_file.name)
    tmp_file.close()

    try:
        with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for dirpath, dirs, files in os.walk(resolved_path):
                root_path = Path(dirpath)
                # Symlinks are banned from workspaces (see
                # chat/workspace_symlinks.py); refuse rather than let
                # ZipFile.write dereference one into the archive.
                if any(
                    (root_path / name).is_symlink()
                    for name in (*dirs, *files)
                ):
                    raise ValueError(
                        "Cannot archive a folder containing symbolic links"
                    )
                # Archive path: folder_name/relative/path/...
                arcname_base = folder_name / root_path.relative_to(resolved_path)
                # Add directory entry (handles empty directories)
                zf.write(root_path, arcname_base)
                for filename in files:
                    file_full = root_path / filename
                    # os.walk lists FIFOs/sockets/devices under ``files``;
                    # ZipFile.write would block opening a writer-less FIFO.
                    if not file_full.is_file():
                        raise ValueError(
                            "Cannot archive a folder containing special files"
                        )
                    arc_name = arcname_base / filename
                    zf.write(file_full, arc_name)
    except Exception:
        # Clean up partial temp file on failure
        temp_zip_path.unlink(missing_ok=True)
        raise

    return temp_zip_path, folder_name


def count_workspace_item_files(root: Path, relative_path: str) -> Dict:
    """Get file count info for a file or folder in the workspace.

    Args:
        root: Browsable workspace root (the directory the user sees as "/")
        relative_path: Relative path within workspace

    Returns:
        Dictionary with name, type, and count

    Raises:
        ValueError: If path validation fails
        FileNotFoundError: If path doesn't exist
    """
    is_valid, resolved_path = validate_path(root, relative_path)

    if not is_valid or resolved_path is None:
        raise ValueError("Invalid path")

    if not resolved_path.exists():
        raise FileNotFoundError(f"Not found: {relative_path}")

    if resolved_path.is_file():
        return {
            "name": resolved_path.name,
            "type": "file",
            "count": 1,
        }

    # For folders, count all files recursively (not subdirectories)
    file_count = sum(1 for p in resolved_path.rglob("*") if p.is_file())
    return {
        "name": resolved_path.name,
        "type": "folder",
        "count": file_count,
    }


def delete_workspace_item(root: Path, relative_path: str) -> Dict:
    """Delete a file or folder from the workspace.

    Args:
        root: Browsable workspace root (the directory the user sees as "/")
        relative_path: Relative path within workspace

    Returns:
        Dictionary with name, type, and deleted_count

    Raises:
        ValueError: If path validation fails or attempting to delete workspace root
        FileNotFoundError: If path doesn't exist
    """
    is_valid, resolved_path = validate_path(root, relative_path)

    if not is_valid or resolved_path is None:
        raise ValueError("Invalid path")


    # Prevent deleting the workspace root itself
    if resolved_path.resolve() == root.resolve():
        raise ValueError("Cannot delete the workspace root directory")

    if not resolved_path.exists():
        raise FileNotFoundError(f"Not found: {relative_path}")

    if resolved_path.is_file():
        resolved_path.unlink()
        return {
            "name": resolved_path.name,
            "type": "file",
            "deleted_count": 1,
        }

    # For folders, count files before deleting
    file_count = sum(1 for p in resolved_path.rglob("*") if p.is_file())
    shutil.rmtree(resolved_path)
    return {
        "name": resolved_path.name,
        "type": "folder",
        "deleted_count": file_count,
    }


# ---------------------------------------------------------------------------
# Copying entries between workspace roots (conversation <-> project)
# ---------------------------------------------------------------------------

# First path components that are scratch space by convention and are never
# promoted out of a conversation workspace (see ``is_scratch_source``).
SCRATCH_SOURCE_ROOTS = frozenset({".responses", ".subagent_responses", "pasted"})


class CopyEntryError(Exception):
    """Structured refusal from ``copy_entry``.

    ``code`` is one of ``invalid_path``, ``not_found``,
    ``not_a_regular_file``, ``destination_exists``, ``invalid_destination``
    or ``forbidden_source``; callers map it onto their own error shape.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _clean_rel(rel: str) -> str:
    """Strip the leading separators and trailing slashes ``validate_path`` ignores."""
    return (rel or "").replace("\\", "/").strip("/")


def is_scratch_source(rel: str) -> bool:
    """True when ``rel`` lies in a conversation-only scratch root.

    ``.responses/``, ``.subagent_responses/`` and ``pasted/`` (and anything
    under them) hold tool-call response bodies, sub-agent deliveries and
    composer pastes tied to one conversation; copy-to-project refuses them
    as a source.
    """
    parts = [p for p in _clean_rel(rel).split("/") if p and p != "."]
    return bool(parts) and parts[0] in SCRATCH_SOURCE_ROOTS


def _resolve_copy_end(root: Path, rel: str, *, side: str) -> Tuple[Path, str]:
    """Validate one end of a copy and return ``(unresolved_path, clean_rel)``.

    ``validate_path`` rejects ``..`` and containment escapes; on top of that
    no existing component below ``root`` may be a symlink (the unresolved
    path is what gets lstat'ed and opened, so a link can never redirect the
    copy). The root itself is not a valid end.
    """
    code = "invalid_path" if side == "source" else "invalid_destination"
    clean = _clean_rel(rel)
    if not clean or clean == ".":
        raise CopyEntryError(
            code, f"The {side} path must name an entry, not the workspace root"
        )
    is_valid, resolved = validate_path(root, clean)
    if not is_valid or resolved is None:
        raise CopyEntryError("invalid_path", f"Invalid {side} path: {rel}")
    if resolved == root.resolve():
        raise CopyEntryError(
            code, f"The {side} path must name an entry, not the workspace root"
        )
    current = root
    for part in Path(clean).parts:
        current = current / part
        if current.is_symlink():
            raise CopyEntryError(
                "not_a_regular_file" if side == "source" else "invalid_destination",
                f"'{rel}' passes through a symbolic link",
            )
        if not os.path.lexists(current):
            break
    return root / clean, clean


def _entry_kind(path: Path) -> Optional[str]:
    """``"file"`` / ``"dir"`` / ``"symlink"`` / ``"special"``, or None if absent."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "special"


# Deepest folder nesting below the copied directory that copy_entry accepts.
MAX_COPY_DEPTH = 256


def _plan_dir(
    src_dir: Path, dst_dir: Path, include_hidden: bool,
    files: list, dirs: list,
) -> int:
    """Collect the dirs to create and the files to copy below ``src_dir``.

    Walks without following symlinks. Symlinks, special files and (unless
    ``include_hidden``) dot-named entries are skipped; returns how many.
    Iterative (explicit stack), so a deep tree cannot exhaust the Python
    recursion limit, and nesting deeper than ``MAX_COPY_DEPTH`` is refused
    with ``invalid_path`` before anything is written. Every directory is
    appended to ``dirs`` before anything below it.
    """
    skipped = 0
    stack = [(src_dir, dst_dir, 0)]
    while stack:
        cur_src, cur_dst, depth = stack.pop()
        if depth > MAX_COPY_DEPTH:
            raise CopyEntryError(
                "invalid_path",
                f"Folder nesting is deeper than {MAX_COPY_DEPTH} levels",
            )
        with os.scandir(cur_src) as it:
            entries = sorted(it, key=lambda e: e.name)
        subdirs = []
        for entry in entries:
            if entry.name.startswith(".") and not include_hidden:
                skipped += 1
                continue
            src = cur_src / entry.name
            dst = cur_dst / entry.name
            if entry.is_symlink():
                skipped += 1
            elif entry.is_dir(follow_symlinks=False):
                dirs.append(dst)
                subdirs.append((src, dst, depth + 1))
            elif entry.is_file(follow_symlinks=False):
                files.append((src, dst))
            else:
                skipped += 1
        # Reversed so the alphabetically first subdir is walked first.
        stack.extend(reversed(subdirs))
    return skipped


def _is_under(path: str, root_resolved: Path) -> bool:
    """True when the real ``path`` equals ``root_resolved`` or lies below it.

    A plain string-prefix test on two already-resolved paths (cheaper than
    ``Path.is_relative_to`` on deep trees).
    """
    root = str(root_resolved)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _fd_realpath(fd: int, fallback: str) -> str:
    """Real path of the open descriptor ``fd``.

    Uses ``/proc/self/fd`` (Linux), which reports what the descriptor
    actually refers to even if a path component was swapped for a symlink
    after the open. Without /proc it falls back to ``realpath(fallback)``,
    which only sees the path name as it is now.
    """
    proc = f"/proc/self/fd/{fd}"
    if os.path.isdir("/proc/self/fd"):
        try:
            return os.path.realpath(proc)
        except OSError:
            pass
    return os.path.realpath(fallback)


def _require_under(real: str, root_resolved: Path, code: str, what: str) -> None:
    """Raise ``CopyEntryError(code)`` unless ``real`` is inside ``root_resolved``."""
    if not _is_under(real, root_resolved):
        raise CopyEntryError(
            code, f"{what} moved outside the workspace during the copy",
        )


def _copy_regular_file(
    src: Path, dst: Path, *, src_root: Path, dst_root: Path,
    exclusive: bool = False,
) -> None:
    """Copy one regular file without following a symlink on either end.

    The source is opened ``O_NOFOLLOW | O_NONBLOCK`` and re-checked on the
    descriptor (a FIFO swapped in after planning can't block the thread),
    and the descriptor's real path must lie under ``src_root``; the bytes go
    to a temp file beside ``dst`` whose real parent must lie under
    ``dst_root`` (both catch a directory swapped for a symlink mid-copy,
    ``invalid_path``). The temp file is renamed over ``dst``, so an
    existing destination is replaced atomically and never written through;
    with ``exclusive`` it is hard-linked instead, so a destination that
    appeared concurrently is ``destination_exists`` and never replaced.
    Permission bits (masked to ``0o777``: no setuid/setgid/sticky) and
    mtime are preserved like ``shutil.copy2``.
    """
    src_root_resolved = src_root.resolve()
    dst_root_resolved = dst_root.resolve()
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise CopyEntryError(
                "not_a_regular_file", f"'{src.name}' is not a regular file"
            )
        _require_under(
            _fd_realpath(fd, str(src)), src_root_resolved, "invalid_path",
            f"Source '{src.name}'",
        )
        os.set_blocking(fd, True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f".{dst.name}.", suffix=".copytmp", dir=dst.parent,
        )
        try:
            _require_under(
                os.path.dirname(_fd_realpath(tmp_fd, tmp_name)),
                dst_root_resolved, "invalid_path", f"Destination of '{dst.name}'",
            )
            with os.fdopen(tmp_fd, "wb") as out, os.fdopen(fd, "rb") as inp:
                tmp_fd = fd = -1  # ownership passed to the file objects
                shutil.copyfileobj(inp, out, 1024 * 1024)
            os.chmod(tmp_name, stat.S_IMODE(st.st_mode) & 0o777)
            os.utime(tmp_name, ns=(st.st_atime_ns, st.st_mtime_ns))
            if exclusive:
                _link_exclusive(tmp_name, dst)
            else:
                os.replace(tmp_name, dst)
        except BaseException:
            if tmp_fd >= 0:
                os.close(tmp_fd)
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
    finally:
        if fd >= 0:
            os.close(fd)


def _link_exclusive(tmp_name: str, dst: Path) -> None:
    """Move ``tmp_name`` to ``dst`` only if ``dst`` does not exist yet.

    A hard link fails atomically on an existing name; on a filesystem
    without hard links it degrades to an existence check + rename.
    """
    try:
        os.link(tmp_name, dst, follow_symlinks=False)
    except FileExistsError:
        raise CopyEntryError(
            "destination_exists", f"'{dst.name}' already exists"
        ) from None
    except OSError:
        if os.path.lexists(dst):
            raise CopyEntryError(
                "destination_exists", f"'{dst.name}' already exists"
            ) from None
        os.replace(tmp_name, dst)
        return
    try:
        os.unlink(tmp_name)
    except OSError:
        logger.warning("copy_entry: could not remove temp file %s", tmp_name)


def _remove_moved_source(
    src: Path, kind: str, copied: list, src_root: Path,
) -> None:
    """Delete exactly what was copied, then prune emptied directories.

    Entries the copy skipped (dot-entries, symlinks, special files) stay in
    the source, and so does any directory still holding them -- a move
    never destroys data it did not deliver. Before each delete the real
    path of the containing directory must still lie under ``src_root``
    (``invalid_path`` otherwise, stopping the removal), so a directory
    swapped for a symlink mid-move cannot redirect a delete.
    """
    root_resolved = src_root.resolve()
    if kind == "file":
        _require_under(
            os.path.realpath(src.parent), root_resolved, "invalid_path",
            f"Source '{src.name}'",
        )
        src.unlink()
        return
    for file_src, _dst in copied:
        _require_under(
            os.path.realpath(file_src.parent), root_resolved, "invalid_path",
            f"Source '{file_src.name}'",
        )
        file_src.unlink()
    for dirpath, _dirnames, _filenames in sorted(
        os.walk(src), key=lambda t: len(Path(t[0]).parts), reverse=True,
    ):
        _require_under(
            os.path.realpath(dirpath), root_resolved, "invalid_path",
            f"Source folder '{Path(dirpath).name}'",
        )
        try:
            os.rmdir(dirpath)
        except OSError:
            pass  # not empty: holds skipped entries


def _cleanup_failed_copy(
    dst_path: Path, created_top: bool, created_parents: list,
) -> None:
    """Undo what a failed copy into a fresh destination created.

    Removes the top-level destination only when this copy created it, then
    rmdirs the ancestor dirs it created (deepest first, only while empty).
    A merge into an existing destination cannot be undone.
    """
    if created_top:
        if dst_path.is_dir() and not dst_path.is_symlink():
            shutil.rmtree(dst_path, ignore_errors=True)
        else:
            try:
                dst_path.unlink()
            except OSError:
                pass
    for parent in reversed(created_parents):
        try:
            os.rmdir(parent)
        except OSError:
            break  # not empty (or gone): stop pruning


def copy_entry(
    src_root: Path,
    src_rel: str,
    dst_root: Path,
    dst_rel: str,
    *,
    overwrite: bool,
    include_hidden: bool,
    move: bool,
) -> Dict:
    """Copy (or move) a file or directory from one workspace root to another.

    Sync; callers run it in ``asyncio.to_thread``. Both roots are browsable
    roots (``ChatStorage.get_conversation_workspace_root`` /
    ``get_project_workspace_root``); ``dst_rel`` is required -- callers
    default it to ``src_rel``. A leading ``/`` is root-relative on both
    ends, as on every other file route.

    Rules (devplan 00009 sections 6.3 / 10.8):

    * Each end is validated against its own root (``..``, containment
      escapes and symlink components refused); neither may be the root.
    * The source must exist and be a regular file or a directory; a
      symlink or special file (FIFO, socket, device) named as the source is
      refused with ``not_a_regular_file``.
    * A directory is walked without following symlinks; symlinks and
      special files inside it are skipped (not refused), and dot-named
      entries at every depth are skipped unless ``include_hidden``. A
      dot-named entry given explicitly as ``src_rel`` is copied.
    * An existing destination without ``overwrite`` is
      ``destination_exists``. With ``overwrite`` a file replaces a file and
      a directory merges entry by entry (unrelated destination entries
      kept). A type mismatch -- file onto a directory, directory onto a
      file, at the top or anywhere in the merge -- or a symlink / special
      file in the way is ``invalid_destination``. Every conflict is found
      before anything is written.
    * A destination equal to the source, inside its subtree, or (both ends
      in the same root) an ancestor of it is ``invalid_destination``.
    * Missing destination parents are created (a parent that exists as a
      file is ``invalid_destination``). A fresh top-level destination is
      created exclusively: one that appears concurrently is
      ``destination_exists``, never merged into or replaced.
    * Mid-copy symlink swaps are caught: every opened source and every
      written destination directory must still really lie inside its root
      (``invalid_path``, see ``_copy_regular_file``).
    * On a failure into a fresh destination, everything this call created
      (the destination and any parents) is removed again.
    * Copied files keep their permission bits masked to ``0o777``.
    * ``move`` removes the source only after the whole copy succeeded, and
      only what was copied (see ``_remove_moved_source``). If that removal
      fails, the copy still stands: the result reports ``moved: false`` and
      the failure is logged.

    The ``.responses/`` / ``.subagent_responses/`` / ``pasted/`` scratch
    refusal is the copy-to-project caller's job (``is_scratch_source``).

    Returns:
        ``{"type": "file" | "folder", "path": "/<dst_rel>",
        "files_copied": int, "skipped": int, "moved": bool}``.

    Raises:
        CopyEntryError: on any refusal; for refusals found before writing
            (all but ``invalid_path`` mid-copy and a concurrent
            ``destination_exists``) nothing has been written.
        OSError: an I/O failure while copying (a fresh destination is
            cleaned up as above; a merge may be partial).
    """
    src_path, _src_clean = _resolve_copy_end(src_root, src_rel, side="source")
    dst_path, dst_clean = _resolve_copy_end(dst_root, dst_rel, side="destination")

    src_kind = _entry_kind(src_path)
    if src_kind is None:
        raise CopyEntryError("not_found", f"Not found: {src_rel}")
    if src_kind == "symlink":
        raise CopyEntryError(
            "not_a_regular_file", f"'{src_rel}' is a symbolic link"
        )
    if src_kind == "special":
        raise CopyEntryError(
            "not_a_regular_file", f"'{src_rel}' is not a regular file or folder"
        )

    src_root_resolved = src_root.resolve()
    dst_root_resolved = dst_root.resolve()
    src_resolved = src_path.resolve()
    dst_resolved = dst_path.resolve()
    if dst_resolved == src_resolved or src_resolved in dst_resolved.parents:
        raise CopyEntryError(
            "invalid_destination",
            "The destination is the source itself or inside it",
        )
    if src_root_resolved == dst_root_resolved and dst_resolved in src_resolved.parents:
        raise CopyEntryError(
            "invalid_destination",
            "The destination is a folder that contains the source",
        )

    # Parents: every existing ancestor below the root must be a directory.
    parent = dst_root
    for part in Path(dst_clean).parts[:-1]:
        parent = parent / part
        if _entry_kind(parent) not in (None, "dir"):
            raise CopyEntryError(
                "invalid_destination",
                f"Cannot create folder '{part}': "
                "a file with that name already exists",
            )

    dst_kind = _entry_kind(dst_path)
    if dst_kind is not None and not overwrite:
        raise CopyEntryError(
            "destination_exists", f"'{dst_clean}' already exists"
        )
    if dst_kind is not None and dst_kind != src_kind:
        raise CopyEntryError(
            "invalid_destination",
            f"'{dst_clean}' exists and is not a "
            f"{'folder' if src_kind == 'dir' else 'regular file'}",
        )

    # Plan the whole copy, then check every planned target, before writing.
    files: list = []
    dirs: list = []
    skipped = 0
    if src_kind == "file":
        files.append((src_path, dst_path))
    else:
        dirs.append(dst_path)
        skipped = _plan_dir(src_path, dst_path, include_hidden, files, dirs)
        if dst_kind is not None:
            for d in dirs[1:]:
                if _entry_kind(d) not in (None, "dir"):
                    raise CopyEntryError(
                        "invalid_destination",
                        f"'{d.relative_to(dst_root)}' exists and is not a folder",
                    )
            for _s, f in files:
                if _entry_kind(f) not in (None, "file"):
                    raise CopyEntryError(
                        "invalid_destination",
                        f"'{f.relative_to(dst_root)}' exists and is not "
                        "a regular file",
                    )

    fresh = dst_kind is None
    created_top = False
    created_parents: list = []

    def _mkdir_checked(path: Path, *, exist_ok: bool) -> bool:
        """mkdir ``path`` after checking its parent really is in dst_root."""
        _require_under(
            os.path.realpath(path.parent), dst_root_resolved, "invalid_path",
            f"Folder '{path.parent.name or '/'}'",
        )
        try:
            os.mkdir(path)
        except FileExistsError:
            if not exist_ok:
                raise
            return False
        return True

    try:
        current = dst_root
        for part in Path(dst_clean).parts[:-1]:
            current = current / part
            if _mkdir_checked(current, exist_ok=True):
                created_parents.append(current)
        if src_kind == "dir":
            if fresh:
                try:
                    _mkdir_checked(dst_path, exist_ok=False)
                except FileExistsError:
                    raise CopyEntryError(
                        "destination_exists", f"'{dst_clean}' already exists"
                    ) from None
                created_top = True
            for d in dirs[1:]:
                _mkdir_checked(d, exist_ok=True)
        for s, f in files:
            _copy_regular_file(
                s, f, src_root=src_root, dst_root=dst_root,
                exclusive=fresh and f == dst_path,
            )
    except BaseException:
        # A fresh destination is removed again so a failed copy leaves
        # nothing behind; a merge into an existing one cannot be undone.
        try:
            _cleanup_failed_copy(dst_path, created_top, created_parents)
        except Exception:
            logger.warning(
                "copy_entry: cleanup of failed copy to %s failed", dst_path,
                exc_info=True,
            )
        raise

    moved = False
    if move:
        try:
            _remove_moved_source(src_path, src_kind, files, src_root)
            moved = True
        except (OSError, CopyEntryError) as exc:
            logger.warning(
                "copy_entry: copied %s to %s but removing the source failed: %s",
                src_path, dst_path, exc,
            )

    return {
        "type": "folder" if src_kind == "dir" else "file",
        "path": "/" + dst_clean,
        "files_copied": len(files),
        "skipped": skipped,
        "moved": moved,
    }
