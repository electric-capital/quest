"""Resolution and enumeration helpers for system skills.

These functions are the only entry points the rest of the codebase should
use; the catalog itself is treated as private state.
"""

from typing import Optional

from chat.docs.constants import DOCS_SERVICE_KEY, docs_disabled_message
from chat.system_skills.catalog import CATALOG, SYSTEM_SKILL_PREFIX, SystemSkill


def is_system_skill_id(skill_id: str) -> bool:
    """Return True if ``skill_id`` uses the reserved ``system:`` prefix."""
    return isinstance(skill_id, str) and skill_id.startswith(SYSTEM_SKILL_PREFIX)


def _is_visible(
    skill: SystemSkill,
    connected_services: Optional[dict[str, bool]],
    has_project: bool,
) -> bool:
    """Return True if a skill should be visible to the agent.

    A skill is visible when its ``requires`` gate (if any) is satisfied
    and, if it is project-scoped, the conversation belongs to a project.
    Passing ``connected_services=None`` is treated as "include all
    backend skills" (used by tests and admin tooling).
    """
    if skill.requires_project and not has_project:
        return False
    if skill.requires is None:
        return True
    if connected_services is None:
        return True
    return bool(connected_services.get(skill.requires, False))


def list_system_skills(
    connected_services: Optional[dict[str, bool]] = None,
    has_project: bool = False,
) -> list[SystemSkill]:
    """Return the system skills available for the current context.

    Args:
        connected_services: Output of ``get_user_connected_services``.
            ``None`` means "include all backend skills" (admin / test).
        has_project: Whether the current conversation belongs to a project.

    Returns:
        A list of :class:`SystemSkill` entries in catalog order.
    """
    return [
        skill for skill in CATALOG.values()
        if _is_visible(skill, connected_services, has_project)
    ]


def load_system_skills(
    skill_ids: list[str],
    connected_services: Optional[dict[str, bool]],
    base_url: str,
    api_key: str,
    has_project: bool = False,
) -> list[dict]:
    """Resolve full content for the requested system skill ids.

    Each returned entry has ``id``, ``name``, ``description``, and either
    ``content`` (success) or ``error`` (gate failure). Unknown
    ``system:`` ids are silently skipped — callers should partition ids
    via :func:`is_system_skill_id` before calling this.

    Args:
        skill_ids: A list of ``system:*`` ids. Non-system ids are ignored.
        connected_services: User's connected-services map. ``None`` means
            "all gates pass".
        base_url: Quest API proxy base URL (passed to content builders).
        api_key: User's API key (passed to content builders that need it).
        has_project: Whether the current conversation belongs to a project.

    Returns:
        A list of result dicts. The order matches the input order.
    """
    results: list[dict] = []
    for raw_id in skill_ids:
        if not is_system_skill_id(raw_id):
            continue
        skill = CATALOG.get(raw_id)
        if skill is None:
            # Silently skip — model may have hallucinated an id.
            continue
        if skill.requires_project and not has_project:
            results.append({
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "visibility": "system",
                "error": (
                    "Project DB is only available inside a project conversation. "
                    "Move this conversation into a project (or create one) to use it."
                ),
            })
            continue
        if skill.requires is not None and connected_services is not None and not connected_services.get(skill.requires, False):
            if skill.requires == DOCS_SERVICE_KEY:
                # A feature-gate pseudo-key, not a connector (see
                # api/instructions.py PSEUDO_SERVICE_KEYS).
                error = docs_disabled_message()
            else:
                error = (
                    f"{skill.name} is not connected. Connect it in "
                    "Settings > Data Connections, then retry."
                )
            results.append({
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "visibility": "system",
                "error": error,
            })
            continue
        try:
            content = skill.content_builder(base_url, api_key)
        except Exception as exc:  # pragma: no cover -- defensive
            results.append({
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "visibility": "system",
                "error": f"Failed to build skill content: {exc}",
            })
            continue
        results.append({
            "id": skill.id,
            "name": skill.name,
            "description": skill.description,
            "visibility": "system",
            "content": content,
        })
    return results


def build_system_skills_enumeration(
    connected_services: Optional[dict[str, bool]] = None,
    has_project: bool = False,
) -> str:
    """Build the compact "System Skills" block for the system prompt.

    Returns a Markdown-formatted bullet list, one line per visible skill,
    or an empty string if no skills are visible (e.g. brand-new user
    with no services connected and no project).
    """
    visible = list_system_skills(connected_services, has_project)
    if not visible:
        return ""

    header = (
        "**System Skills (loadable on demand via `load_skills` -- do NOT "
        "try to use a backend's APIs before loading the relevant system "
        "skill first):**\n\n"
        "The following built-in skills contain detailed API docs for each "
        "backend or topic. They are NOT loaded automatically -- call "
        "`load_skills(skill_ids=[...])` with the skill id(s) when you "
        "need them. You can load multiple at once. Loaded skill content "
        "stays in the conversation for the rest of the turn (and remains "
        "visible to you in subsequent turns of the same conversation)."
    )

    lines = [f"- `{skill.id}` -- {skill.description} {skill.when_to_load}".rstrip() for skill in visible]
    return header + "\n\n" + "\n".join(lines)
