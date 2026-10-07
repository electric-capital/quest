"""File storage utilities for workspace file browser."""

import errno
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
