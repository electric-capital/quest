"""Resolution and enumeration helpers for system skills.

These functions are the only entry points the rest of the codebase should
use; the catalog itself is treated as private state.
"""

import inspect
from typing import Callable, Optional

from chat.browser_sessions import BROWSER_SERVICE_KEY, browser_disabled_message
from chat.docs.constants import DOCS_SERVICE_KEY, docs_disabled_message
from chat.tables.constants import TABLES_SERVICE_KEY, tables_disabled_message
from chat.llm.tool_schemas import (
    TOOL_CALL_REGISTRY,
    render_tool_reference,
    tool_is_advertised,
    tools_for_skill,
)
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


_ACCEPTS_CONNECTIONS_CACHE: dict[int, tuple[Callable, bool]] = {}


def builder_accepts_connections(builder: Callable) -> bool:
    """Whether a skill content builder takes a ``connections`` keyword.

    Existing ``(base_url, api_key)`` builders (and lambdas) do not, and are
    called exactly as before. Cached per builder object.
    """
    cached = _ACCEPTS_CONNECTIONS_CACHE.get(id(builder))
    if cached is not None and cached[0] is builder:
        return cached[1]
    try:
        params = inspect.signature(builder).parameters
    except (TypeError, ValueError):
        accepts = False
    else:
        param = params.get("connections")
        if param is not None:
            accepts = param.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        else:
            accepts = any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
    # Hold a reference to the builder so its id() can never be reused.
    _ACCEPTS_CONNECTIONS_CACHE[id(builder)] = (builder, accepts)
    return accepts


def build_skill_content(
    skill: SystemSkill, base_url: str, api_key: str, user: Optional[dict] = None,
) -> str:
    """Run a skill's content builder.

    Builders that accept a ``connections`` keyword also receive
    ``config.user_connections.connection_summary(user, skill.requires)`` (an
    empty list without a user or a ``requires`` service), so a
    multi-connection plugin's skill can show the user's real account
    addresses. Every other builder is called as ``(base_url, api_key)``.
    """
    if builder_accepts_connections(skill.content_builder):
        from config.user_connections import connection_summary

        connections = (
            connection_summary(user, skill.requires)
            if user and skill.requires else []
        )
        content = skill.content_builder(base_url, api_key, connections=connections)
    else:
        content = skill.content_builder(base_url, api_key)
    # The skill's prose is the usage guidance; the registry specs are the
    # parameter truth, so they are rendered from the registry rather than
    # copied into the prose (the prompt documents only the core tools).
    reference = render_tool_reference(skill.id)
    if reference:
        content = content.rstrip() + "\n\n" + reference
    return content


def load_system_skills(
    skill_ids: list[str],
    connected_services: Optional[dict[str, bool]],
    base_url: str,
    api_key: str,
    has_project: bool = False,
    user: Optional[dict] = None,
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
        user: The user dict, for builders that take a ``connections``
            keyword (see :func:`build_skill_content`). Optional.

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
            elif skill.requires == TABLES_SERVICE_KEY:
                error = tables_disabled_message()
            elif skill.requires == BROWSER_SERVICE_KEY:
                error = browser_disabled_message()
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
            content = build_skill_content(skill, base_url, api_key, user)
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


def skill_tool_names(
    skill: SystemSkill,
    connected_services: Optional[dict[str, bool]],
    exclude_tools: Optional[set[str]] = None,
) -> list[str]:
    """The ``tool_call`` tools a prompt may name on *skill*'s enumeration
    line: the registry tools whose ``skill`` is this skill, minus the
    prompt's excluded tools and those gated on an unconnected service."""
    exclude = exclude_tools or set()
    return [
        name for name in tools_for_skill(skill.id)
        if name not in exclude
        and tool_is_advertised(TOOL_CALL_REGISTRY[name], connected_services)
    ]


def build_system_skills_enumeration(
    connected_services: Optional[dict[str, bool]] = None,
    has_project: bool = False,
    exclude_tools: Optional[set[str]] = None,
    include_only: Optional[frozenset[str]] = None,
) -> str:
    """Build the compact "System Skills" block for the system prompt.

    Returns a Markdown-formatted bullet list, one line per visible skill,
    or an empty string if no skills are visible (e.g. brand-new user
    with no services connected and no project). A skill's line ends with
    the names of the non-core ``tool_call`` tools it documents, so the
    prompt's Dynamic Tools section can document the core tools alone;
    ``exclude_tools`` is the prompt's excluded-tool set (the ``exclude``
    its builder passes to ``_build_dynamic_tools_section``), so a tool
    hidden from the prompt is not named here either. ``include_only``
    restricts the block to those skill ids (the public prompt passes
    ``PUBLIC_SYSTEM_SKILLS``); the gates still apply.
    """
    visible = list_system_skills(connected_services, has_project)
    if include_only is not None:
        visible = [skill for skill in visible if skill.id in include_only]
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
        "visible to you in subsequent turns of the same conversation). "
        "A skill's `Tools:` list names the `tool_call` tools it documents; "
        "load the skill before calling one of them -- its content ends with "
        "a \"Tool reference\" giving their parameters."
    )

    lines = []
    for skill in visible:
        line = f"- `{skill.id}` -- {skill.description} {skill.when_to_load}".rstrip()
        names = skill_tool_names(skill, connected_services, exclude_tools)
        if names:
            line += " Tools: " + ", ".join(f"`{n}`" for n in names) + "."
        lines.append(line)
    return header + "\n\n" + "\n".join(lines)
