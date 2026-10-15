"""Helpers for asserting which ``tool_call`` tools a system prompt offers.

A prompt offers a dynamic tool in one of two forms (see
``_build_dynamic_tools_section`` and ``build_system_skills_enumeration``):
a core tool gets a full ``- **name** --`` bullet in the Dynamic Tools
section; every other tool is named in the ``Tools:`` tail of the System
Skill line that documents it. The public-project prompt, which has no
skill tools, gives every offered tool the bullet form.
"""

import re

_SKILL_LINE_RE = re.compile(r"^- `(system:[^`]+)` -- .*?(?: Tools: (.*))?$", re.M)


def core_bullet(name: str) -> str:
    """The Dynamic Tools bullet documenting a core (or public-prompt) tool."""
    return f"- **{name}** --"


def skill_tools(prompt: str, skill_id: str) -> list[str] | None:
    """The tool names on *skill_id*'s enumeration line, or None when the
    prompt does not enumerate that skill."""
    for match in _SKILL_LINE_RE.finditer(prompt):
        if match.group(1) == skill_id:
            tail = match.group(2) or ""
            return re.findall(r"`([^`]+)`", tail)
    return None


def named_under_skill(prompt: str, skill_id: str, name: str) -> bool:
    """Whether *name* is listed on *skill_id*'s enumeration line."""
    return name in (skill_tools(prompt, skill_id) or [])


def tool_offered(prompt: str, name: str) -> bool:
    """Whether the prompt offers *name* in either form."""
    if core_bullet(name) in prompt:
        return True
    return any(
        name in re.findall(r"`([^`]+)`", match.group(2) or "")
        for match in _SKILL_LINE_RE.finditer(prompt)
    )
