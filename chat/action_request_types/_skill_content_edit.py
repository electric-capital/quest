"""Search-and-replace content edits shared by skills and Quest Docs.

``edit_skill`` edits a skill's body the same way ``edit_workspace_file``
edits a workspace file: an exact ``old_string`` -> ``new_string``
replacement (unique match unless ``replace_all``), never a full-content
overwrite. Quest Docs' ``edit_doc`` tool and the ``write_doc`` action
request (``operation: "edit"``) reuse the same primitive through
``chat/docs/service.py``, passing their own size cap, noun ("doc") and
re-read hint ("re-read it with read_doc"). This module holds the two pieces
shared by the proposal-time pre-card checks
(`chat/action_request_types/skill_precard.py`, the doc pre-card) and the
approve-time executes (`chat/action_request_types/edit_skill.py`,
``write_doc``):

- :func:`apply_content_edit` performs the replacement against a given
  current content, raising ``ValueError`` with a model-actionable message
  on a failed / ambiguous match or an oversized result. Running it at both
  proposal and execute time closes the TOCTOU window -- if the skill (or
  doc) changed while the card sat open, the stale ``old_string`` no longer
  matches and the approve fails instead of clobbering the newer content.
- :func:`build_content_diff` turns the old/new content pair into the
  line-diff structure the approval card renders (a changed-hunks snippet
  expandable to the whole body). The full line list is injected into
  the request params as the server-only ``content_diff`` key so the card
  can render without re-fetching the skill or doc.
"""

from difflib import SequenceMatcher

from db.skill_store import MAX_SKILL_CONTENT_SIZE


def apply_content_edit(
    current_content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    *,
    max_size: int = MAX_SKILL_CONTENT_SIZE,
    noun: str = "skill",
    reread_hint: str = "re-read it with get_skill",
) -> tuple[str, int]:
    """Apply an exact string replacement to a skill's (or doc's) content.

    Args:
        current_content: The current body.
        old_string: Exact text to replace (non-empty, enforced upstream).
        new_string: Replacement text (may be empty to delete the match).
        replace_all: Replace every occurrence instead of requiring a
            unique match.
        max_size: Byte cap on the resulting body (UTF-8).
        noun: What is being edited, as it appears in the error messages
            ("skill" -> "the skill content", "Skills must have ...").
        reread_hint: How the model re-reads the current body, used in the
            not-found message ("re-read it with get_skill").

    The defaults keep every skill message byte-identical; Quest Docs pass
    ``max_size=DOC_MAX_CONTENT_SIZE, noun="doc",
    reread_hint="re-read it with read_doc"``.

    Returns:
        Tuple of (new content, replacement count).

    Raises:
        ValueError: When ``old_string`` is not found, is ambiguous without
            ``replace_all``, or the result is empty / over the size cap.
    """
    # str.count is non-overlapping, matching str.replace.
    occurrences = current_content.count(old_string)
    if occurrences == 0:
        raise ValueError(
            f"old_string not found in the {noun} content. The {noun} may have "
            f"changed -- {reread_hint} and retry with the exact "
            "current text."
        )
    if occurrences > 1 and not replace_all:
        raise ValueError(
            f"old_string appears {occurrences} times in the {noun} content. "
            "Include more surrounding context to make the match unique, or "
            "pass replace_all: true to replace every occurrence."
        )

    replacements = occurrences if replace_all else 1
    new_content = current_content.replace(
        old_string, new_string, -1 if replace_all else 1
    )

    if not new_content.strip():
        raise ValueError(
            f"The edit would leave the {noun} content empty. "
            f"{noun.capitalize()}s must have non-empty content."
        )
    if len(new_content.encode("utf-8")) > max_size:
        raise ValueError(
            f"The edited {noun} content exceeds the maximum size of "
            f"{max_size} bytes."
        )
    return new_content, replacements


def build_content_diff(old_content: str, new_content: str) -> dict:
    """Build the line-diff structure the edit_skill / write_doc approval
    cards render.

    The ``lines`` list covers the ENTIRE old/new content pair (context
    lines included) so the frontend can render both the collapsed
    changed-hunks snippet and the expanded whole-body view from one
    structure. Line numbers are 1-based; context lines carry both, while
    removed/added lines carry only the side they exist on.

    Args:
        old_content: The current body (skill or doc).
        new_content: The body after the replacement.

    Returns:
        Dict with ``added`` / ``removed`` line counts and the full
        ``lines`` list of ``{"type": "context"|"del"|"add", "old_line",
        "new_line", "text"}`` entries.
    """
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()

    lines: list[dict] = []
    added = removed = 0
    matcher = SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                lines.append({
                    "type": "context",
                    "old_line": i1 + offset + 1,
                    "new_line": j1 + offset + 1,
                    "text": old_lines[i1 + offset],
                })
            continue
        # replace / delete / insert: emit removals then additions.
        for i in range(i1, i2):
            removed += 1
            lines.append({
                "type": "del",
                "old_line": i + 1,
                "new_line": None,
                "text": old_lines[i],
            })
        for j in range(j1, j2):
            added += 1
            lines.append({
                "type": "add",
                "old_line": None,
                "new_line": j + 1,
                "text": new_lines[j],
            })

    return {"added": added, "removed": removed, "lines": lines}
