"""Scheme-qualified file paths for the space-aware file tools.

``list_files`` / ``read_file`` / ``write_file`` / ``edit_file`` /
``copy_file`` address a file space explicitly (devplan 00009, revised
2026-10-08):

* ``chat://<rel>`` -- this conversation's workspace;
* ``proj://<rel>`` -- the project workspace shared by every conversation of
  the project.

The scheme is mandatory. This module is the single parser every handler
uses; it never touches the filesystem (containment and symlink checks stay
with the handlers / ``chat.file_storage``).
"""

# Shared wording for tools that take conversation-workspace paths only
# (attachments, run_script, add_doc_image, ...): the schema sentence, and
# the suffix of a not-found error in a project conversation. Defined in the
# dependency-free chat/workspace_hints.py so chat/llm/tool_schemas.py can
# import them too; re-exported here for the handlers.
from chat.workspace_hints import (  # noqa: F401
    PROJECT_COPY_FIRST_SENTENCE,
    PROJECT_COPY_FIRST_SUFFIX,
)

SPACE_CHAT = "chat"
SPACE_PROJECT = "proj"

_SCHEMES = (SPACE_CHAT, SPACE_PROJECT)

_GRAMMAR_HINT = (
    "Use chat://<path> for this conversation's workspace or proj://<path> "
    "for the project workspace (e.g. chat://notes.md, proj://reports/q3.md)."
)


class SpacePathError(ValueError):
    """A path argument that is not a valid scheme-qualified path."""

    def __init__(self, message: str, code: str = "invalid_path"):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_space_path(raw, *, allow_root: bool) -> tuple[str, str]:
    r"""Split ``chat://a/b`` / ``proj://a/b`` into ``(space, rel)``.

    ``rel`` is a normalised POSIX path relative to the space root: empty
    and ``.`` segments and a trailing ``/`` are dropped. ``""`` (the space
    root itself) is accepted only with ``allow_root``. Like the
    ``*_workspace_file`` tools, nothing is stripped (surrounding spaces are
    part of the name) and a backslash is an ordinary filename character,
    except that a remainder starting with ``\`` counts as absolute.

    Raises:
        SpacePathError: non-string, NUL byte, missing or unknown scheme
            (incl. a leading space before it), absolute remainder
            (``chat:///x``), ``..`` anywhere in the remainder (the same rule
            as ``file_storage.validate_path`` and the old tools), or the
            root where a file or folder must be named. The message names
            both schemes.
    """
    if not isinstance(raw, str):
        raise SpacePathError(f"path must be a string. {_GRAMMAR_HINT}")
    if "\x00" in raw:
        raise SpacePathError(f"Invalid path: NUL byte in {raw!r}. {_GRAMMAR_HINT}")
    scheme, sep, remainder = raw.partition("://")
    if not sep:
        raise SpacePathError(
            f"Invalid path {raw!r}: a chat:// or proj:// prefix is required. "
            f"{_GRAMMAR_HINT}"
        )
    if scheme not in _SCHEMES:
        raise SpacePathError(
            f"Invalid path {raw!r}: unknown space {scheme + '://'!r}. "
            f"{_GRAMMAR_HINT}"
        )
    if remainder.startswith(("/", "\\")):
        raise SpacePathError(
            f"Invalid path {raw!r}: the path after {scheme}:// must be "
            f"relative. {_GRAMMAR_HINT}"
        )
    if ".." in remainder:
        raise SpacePathError(
            f"Invalid path {raw!r}: '..' is not allowed in a path. "
            f"{_GRAMMAR_HINT}"
        )
    parts = [part for part in remainder.split("/") if part not in ("", ".")]
    rel = "/".join(parts)
    if not rel and not allow_root:
        raise SpacePathError(
            f"Invalid path {raw!r}: name a file or folder inside the space, "
            f"not the space root. {_GRAMMAR_HINT}"
        )
    return scheme, rel


def format_space_path(space: str, rel: str) -> str:
    """Canonical ``<space>://<rel>`` form (also the read-sidecar key)."""
    return f"{space}://{rel}"
