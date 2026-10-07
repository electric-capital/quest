"""Workspace symlink policy: symlinks are banned from workspace trees.

Workspaces are host-backed directories bind-mounted read-write into the
script sandbox, and host-side consumers (folder zip downloads, workspace
duplication, uploads) operate on workspace entries with the
server's privileges -- a symlink in a workspace could redirect their reads
or writes to arbitrary host paths. Both kinds of workspace are covered:
conversation workspaces (``chats/<id>/workspace/``, standalone and project
conversations alike) and project workspaces
(``projects/<id>/workspace/workspace/``). The sandbox seccomp profile
(chat/gemini_api/sandbox_seccomp.py) blocks creating symlinks at the only
source; this module removes any that predate that fix (or arrive via a
restored backup).
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def remove_symlinks_under(root: Path) -> int:
    """Recursively delete every symlink under ``root``.

    Never follows symlinks while walking: a symlinked directory is removed,
    not descended into (so its target tree is untouched). Iterative walk so
    an adversarially deep tree cannot blow the recursion limit. A missing
    root is a no-op; per-entry failures are logged and skipped.

    Returns:
        The number of symlinks removed.
    """
    removed = 0
    stack = [Path(root)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            logger.exception("Symlink scrub could not list %s", current)
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    try:
                        target = os.readlink(entry.path)
                    except OSError:
                        target = "?"
                    os.unlink(entry.path)
                    removed += 1
                    # Warning level: the app never creates workspace
                    # symlinks, so every removal is evidence of a
                    # pre-seccomp-fix sandbox escape attempt.
                    logger.warning(
                        "Removed banned workspace symlink %s -> %s",
                        entry.path, target,
                    )
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
            except OSError:
                logger.exception("Symlink scrub failed on %s", entry.path)
    return removed


def scrub_workspace_symlinks() -> int:
    """Remove symlinks from every conversation and project data tree.

    Sweeps the whole of ``CHATS_DIR`` and ``PROJECTS_DIR`` (metadata files
    included -- no legitimate symlink exists anywhere under either).
    Called from the quest.py lifespan on every boot; cheap because scandir
    surfaces symlink-ness without extra stat calls.

    Returns:
        The total number of symlinks removed.
    """
    from config import paths

    removed = 0
    for root in (paths.CHATS_DIR, paths.PROJECTS_DIR):
        removed += remove_symlinks_under(root)
    return removed
