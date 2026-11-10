"""Scheme-qualified file paths for the space-aware file tools.

``list_files`` / ``read_file`` / ``write_file`` / ``edit_file`` /
``copy_file`` address a file space explicitly (devplan 00009, revised
2026-10-08; issue #213 for ``task://``):

* ``chat://<rel>`` -- this conversation's workspace;
* ``proj://<rel>`` -- the project workspace shared by every conversation of
  the project;
* ``task://<rel>`` -- the task workspace shared by every task run of a
  Quest Task.

The scheme is mandatory. Which schemes a conversation has depends on its
kind (``conversation_spaces``): a standalone or project conversation has
``chat://`` and ``proj://`` (``DEFAULT_SPACES``; ``proj://`` outside a
project is refused later, by ``workspace.resolve_space_root``, with
``no_project``), a task run has ``chat://`` and ``task://``
(``TASK_RUN_SPACES``). A known scheme the conversation does not have is
refused here: ``proj://`` in a task run with ``no_project``, ``task://``
outside one with ``no_task``. Every message ends with the grammar hint of
the conversation's spaces.

This module is the single parser every handler uses; it never touches the
filesystem (containment and symlink checks stay with the handlers /
``chat.file_storage``).
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
SPACE_TASK = "task"

_SCHEMES = (SPACE_CHAT, SPACE_PROJECT, SPACE_TASK)

# The spaces of a standalone or project conversation, and of a task run.
DEFAULT_SPACES = (SPACE_CHAT, SPACE_PROJECT)
TASK_RUN_SPACES = (SPACE_CHAT, SPACE_TASK)

_GRAMMAR_HINT = (
    "Use chat://<path> for this conversation's workspace or proj://<path> "
    "for the project workspace (e.g. chat://notes.md, proj://reports/q3.md)."
)

_TASK_RUN_GRAMMAR_HINT = (
    "Use chat://<path> for this task run's workspace or task://<path> for "
    "the task workspace shared by every task run of this Quest Task "
    "(e.g. chat://notes.md, task://reports/q3.md)."
)

_GRAMMAR_HINTS = {
    DEFAULT_SPACES: _GRAMMAR_HINT,
    TASK_RUN_SPACES: _TASK_RUN_GRAMMAR_HINT,
}


def conversation_spaces(task_id: str | None) -> tuple[str, ...]:
    """The file spaces of a conversation: ``TASK_RUN_SPACES`` for a task
    run (``task_id`` set), ``DEFAULT_SPACES`` otherwise."""
    return TASK_RUN_SPACES if task_id else DEFAULT_SPACES


def grammar_hint(spaces: tuple[str, ...] = DEFAULT_SPACES) -> str:
    """The sentence telling the model which schemes it has, with examples.

    Raises:
        ValueError: ``spaces`` is not ``DEFAULT_SPACES`` or
            ``TASK_RUN_SPACES``.
    """
    try:
        return _GRAMMAR_HINTS[tuple(spaces)]
    except KeyError:
        raise ValueError(f"unknown file space set: {spaces!r}") from None


class SpacePathError(ValueError):
    """A path argument that is not a valid scheme-qualified path."""

    def __init__(self, message: str, code: str = "invalid_path"):
        super().__init__(message)
        self.code = code
        self.message = message


def _foreign_scheme_error(raw: str, scheme: str, hint: str) -> SpacePathError:
    """Refusal of a known scheme the conversation does not have."""
    if scheme == SPACE_PROJECT:
        return SpacePathError(
            f"Invalid path {raw!r}: a task run has no project workspace, so "
            f"proj:// paths do not exist here. {hint}",
            code="no_project",
        )
    if scheme == SPACE_TASK:
        return SpacePathError(
            f"Invalid path {raw!r}: task:// paths exist only in task runs of "
            f"a Quest Task. {hint}",
            code="no_task",
        )
    # chat:// is in every space set.
    return SpacePathError(
        f"Invalid path {raw!r}: unknown space {scheme + '://'!r}. {hint}"
    )


def parse_space_path(
    raw, *, allow_root: bool, spaces: tuple[str, ...] = DEFAULT_SPACES,
) -> tuple[str, str]:
    r"""Split ``chat://a/b`` / ``proj://a/b`` / ``task://a/b`` into
    ``(space, rel)``.

    ``spaces`` are the schemes the calling conversation has
    (``conversation_spaces``). ``rel`` is a normalised POSIX path relative
    to the space root: empty and ``.`` segments and a trailing ``/`` are
    dropped. ``""`` (the space root itself) is accepted only with
    ``allow_root``. Like the ``*_workspace_file`` tools, nothing is
    stripped (surrounding spaces are part of the name) and a backslash is
    an ordinary filename character, except that a remainder starting with
    ``\`` counts as absolute.

    Raises:
        SpacePathError: non-string, NUL byte, missing or unknown scheme
            (incl. a leading space before it), absolute remainder
            (``chat:///x``), ``..`` anywhere in the remainder (the same rule
            as ``file_storage.validate_path`` and the old tools), or the
            root where a file or folder must be named: code
            ``invalid_path``. A known scheme outside ``spaces``:
            ``no_project`` (``proj://`` in a task run) or ``no_task``
            (``task://`` outside one). The message names the two schemes
            of ``spaces``.
    """
    hint = grammar_hint(spaces)
    if not isinstance(raw, str):
        raise SpacePathError(f"path must be a string. {hint}")
    if "\x00" in raw:
        raise SpacePathError(f"Invalid path: NUL byte in {raw!r}. {hint}")
    scheme, sep, remainder = raw.partition("://")
    if not sep:
        raise SpacePathError(
            f"Invalid path {raw!r}: a {spaces[0]}:// or {spaces[1]}:// prefix "
            f"is required. {hint}"
        )
    if scheme not in _SCHEMES:
        raise SpacePathError(
            f"Invalid path {raw!r}: unknown space {scheme + '://'!r}. "
            f"{hint}"
        )
    if scheme not in spaces:
        raise _foreign_scheme_error(raw, scheme, hint)
    if remainder.startswith(("/", "\\")):
        raise SpacePathError(
            f"Invalid path {raw!r}: the path after {scheme}:// must be "
            f"relative. {hint}"
        )
    if ".." in remainder:
        raise SpacePathError(
            f"Invalid path {raw!r}: '..' is not allowed in a path. "
            f"{hint}"
        )
    parts = [part for part in remainder.split("/") if part not in ("", ".")]
    rel = "/".join(parts)
    if not rel and not allow_root:
        raise SpacePathError(
            f"Invalid path {raw!r}: name a file or folder inside the space, "
            f"not the space root. {hint}"
        )
    return scheme, rel


def format_space_path(space: str, rel: str) -> str:
    """Canonical ``<space>://<rel>`` form (also the read-sidecar key)."""
    return f"{space}://{rel}"
