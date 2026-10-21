"""Skill discovery, loading, and inspection handlers.
"""

import json
import logging

from chat.storage import ChatStorage

logger = logging.getLogger(__name__)


def _mark_skills_read(conversation_id: str | None, skill_ids: list[str]) -> None:
    """Best-effort: record that the model has seen these skills' full
    content in this conversation (loaded or read), licensing later
    edit_skill content edits. Failures never break the read."""
    if not conversation_id or not skill_ids:
        return
    try:
        ChatStorage.add_skill_read_ids(conversation_id, skill_ids)
    except Exception:
        logger.debug(
            "[tool_handlers] failed to record skill read "
            "(conversation_id=%s, skill_ids=%s)",
            conversation_id, skill_ids, exc_info=True,
        )



# ---------------------------------------------------------------------------
# Skill tool handlers
# ---------------------------------------------------------------------------

async def _handle_list_skills(
    user_id: int,
    connected_services: dict[str, bool] | None = None,
    has_project: bool = False,
) -> str:
    """List all skills accessible to the current user.

    Includes both DB-backed user/shared/public skills AND hardcoded
    ``system:*`` skills (gated by ``connected_services`` and
    ``has_project``). System skills appear first.

    Args:
        user_id: User's integer ID.
        connected_services: User's connected-services map (drives system
            skill gating).
        has_project: Whether this conversation belongs to a project.

    Returns:
        JSON string with skill_count and skills array.
    """
    try:
        from chat.system_skills import list_system_skills
        from db.skill_store import list_accessible_skills

        system_skills = list_system_skills(connected_services, has_project)
        db_results = await list_accessible_skills(user_id)
        skills_payload: list[dict] = [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "visibility": "system",
                "when_to_load": s.when_to_load,
            }
            for s in system_skills
        ]
        skills_payload.extend(
            {
                "id": s["id"],
                "name": s["name"],
                "description": s["description"],
                "visibility": s["visibility"],
            }
            for s in db_results
        )
        return json.dumps({
            "skill_count": len(skills_payload),
            "skills": skills_payload,
        })
    except Exception as e:
        return json.dumps({"error": f"Failed to list skills: {e}"})


async def _handle_search_skills(
    user_id: int,
    keyword: str,
    connected_services: dict[str, bool] | None = None,
    has_project: bool = False,
) -> str:
    """Search skills by keyword against name and description.

    Includes both DB-backed skills AND hardcoded ``system:*`` skills
    whose name, description, or when_to_load hint matches the keyword.
    System matches appear first.

    Args:
        user_id: User's integer ID.
        keyword: Search term for substring matching (case-insensitive).
        connected_services: User's connected-services map.
        has_project: Whether this conversation belongs to a project.

    Returns:
        JSON string with match_count and skills array.
    """
    if not keyword or not keyword.strip():
        return json.dumps({"error": "Please provide a keyword to search for."})

    try:
        from chat.system_skills import list_system_skills
        from db.skill_store import search_accessible_skills

        kw = keyword.strip().lower()
        system_matches = [
            s for s in list_system_skills(connected_services, has_project)
            if kw in s.name.lower()
            or kw in s.description.lower()
            or kw in s.when_to_load.lower()
            or kw in s.id.lower()
        ]
        db_results = await search_accessible_skills(user_id, keyword.strip())

        skills_payload: list[dict] = [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "visibility": "system",
                "when_to_load": s.when_to_load,
            }
            for s in system_matches
        ]
        skills_payload.extend(
            {
                "id": s["id"],
                "name": s["name"],
                "description": s["description"],
                "visibility": s["visibility"],
            }
            for s in db_results
        )
        return json.dumps({
            "match_count": len(skills_payload),
            "skills": skills_payload,
        })
    except Exception as e:
        return json.dumps({"error": f"Skill search failed: {e}"})


def _render_loaded_skills_markdown(entries: list[dict]) -> str:
    """Render the ``load_skills`` result set as a single markdown document.

    The document opens with ``# Loaded skills`` and a counts line. When any
    entry has an ``error`` key (system-skill gate failures), a
    ``## Load errors`` section is emitted **before** the successful skill
    blocks so the LLM sees failures first. Each successful entry is rendered
    as its own block separated by ``\\n\\n===\\n\\n`` with a ``# <name>``
    heading, a short metadata header, and a ``## Skill Content`` section
    carrying the raw skill content verbatim (not fenced, since skill content
    is itself markdown prose and may contain fences/headings).

    Args:
        entries: Ordered list of per-skill dicts. Success entries carry
            ``id``, ``name``, ``description``, ``content`` (and optionally
            ``visibility``). Error entries carry ``id``, ``name``,
            ``description``, ``visibility``, and ``error`` (no ``content``).

    Returns:
        Markdown document as a string.
    """
    errors = [e for e in entries if "error" in e]
    successes = [e for e in entries if "error" not in e]

    lines: list[str] = [
        "# Loaded skills",
        "",
        f"Loaded {len(successes)} skill(s). {len(errors)} error(s).",
    ]

    if not entries:
        lines.append("")
        lines.append(
            "_No skills matched the requested ids. "
            "They may not exist or you may not have access._"
        )
        return "\n".join(lines)

    if errors:
        lines.append("")
        lines.append("## Load errors")
        lines.append("")
        for entry in errors:
            sid = entry.get("id", "")
            name = entry.get("name", "")
            description = entry.get("description", "")
            err = entry.get("error", "")
            header_bits = [f"**`{sid}`**"]
            if name:
                if description:
                    header_bits.append(f"{name}: {description}")
                else:
                    header_bits.append(name)
            elif description:
                header_bits.append(description)
            prefix = " -- ".join(header_bits)
            lines.append(f"- {prefix} -- {err}")

    document = "\n".join(lines)

    if successes:
        blocks: list[str] = []
        for entry in successes:
            sid = entry.get("id", "")
            name = entry.get("name", "") or sid
            description = entry.get("description", "")
            visibility = entry.get("visibility", "(unknown)") or "(unknown)"
            content = entry.get("content", "")

            block_lines: list[str] = [
                f"# {name}",
                "",
                f"**ID:** `{sid}`",
                f"**Visibility:** {visibility}",
            ]
            if description:
                block_lines.append(f"**Description:** {description}")
            block_lines.append("")
            block_lines.append("## Skill Content")
            block_lines.append("")
            if content:
                block_lines.append(content)
            else:
                block_lines.append("_(empty content)_")
            blocks.append("\n".join(block_lines))

        document = document + "\n\n===\n\n" + "\n\n===\n\n".join(blocks)

    return document


async def _handle_load_skills(
    user_id: int,
    skill_ids: list[str],
    connected_services: dict[str, bool] | None = None,
    has_project: bool = False,
    base_url: str = "",
    api_key: str = "",
    conversation_id: str | None = None,
    user: dict | None = None,
) -> str:
    """Load the full content of one or more skills by ID.

    Routes ``system:*`` ids to the hardcoded :mod:`chat.system_skills`
    catalog; everything else is resolved against the DB skill store.
    Inaccessible or nonexistent skill IDs are silently skipped (system
    ids whose gate is failed return a structured error entry instead).

    Args:
        user_id: User's integer ID.
        skill_ids: Mixed list of ``system:*`` ids and DB UUIDs.
        connected_services: User's connected-services map (used for
            system skill gating).
        has_project: Whether this conversation belongs to a project.
        base_url: Quest API proxy base URL (for system skill content
            builders).
        api_key: User's API key (for system skill content builders).
        conversation_id: Conversation UUID (records successfully loaded
            DB skills for the edit_skill content-edit read gate).
        user: The user dict, passed to system skill builders that take a
            ``connections`` keyword.

    Returns:
        A markdown document (plain string) describing the loaded skills.
        Empty input and unexpected exceptions still return a single-line
        JSON error envelope (API-contract errors, not load results).
    """
    if not skill_ids:
        return json.dumps({"error": "Please provide at least one skill ID to load."})

    try:
        from chat.system_skills import is_system_skill_id, load_system_skills
        from db.skill_store import get_accessible_skills_by_ids

        system_ids = [sid for sid in skill_ids if is_system_skill_id(sid)]
        db_ids = [sid for sid in skill_ids if not is_system_skill_id(sid)]

        system_results = load_system_skills(
            system_ids, connected_services, base_url, api_key,
            has_project=has_project, user=user,
        ) if system_ids else []

        db_results = await get_accessible_skills_by_ids(user_id, db_ids) if db_ids else []
        _mark_skills_read(conversation_id, [s["id"] for s in db_results])

        skills_payload: list[dict] = list(system_results)
        skills_payload.extend(
            {
                "id": s["id"],
                "name": s["name"],
                "description": s["description"],
                "content": s["content"],
            }
            for s in db_results
        )
        return _render_loaded_skills_markdown(skills_payload)
    except Exception as e:
        return json.dumps({"error": f"Failed to load skills: {e}"})


async def _handle_public_load_skills(
    user: dict, conversation_id: str | None, skill_ids: list[str],
) -> str:
    """``load_skills`` in a PUBLIC-project conversation.

    Admits only ``PUBLIC_SYSTEM_SKILLS`` (core-owned, code-only catalog
    entries whose content names public tools alone); every other id --
    user, shared and project skills, and every other system skill -- is
    refused before any lookup, with the public roster in the error. The
    admitted skills are built with the public connected-services map (so
    a closed feature gate reports its own message) and WITHOUT the proxy
    base URL or the user's API key, which never reach a public prompt.
    """
    from api.instructions import get_public_connected_services
    from chat.llm.tool_schemas import PUBLIC_SYSTEM_SKILLS

    if not isinstance(skill_ids, list) or not skill_ids:
        return json.dumps({"error": "Please provide at least one skill ID to load."})
    refused = [sid for sid in skill_ids if sid not in PUBLIC_SYSTEM_SKILLS]
    if refused:
        return json.dumps({
            "error": (
                f"Skill(s) {', '.join(repr(s) for s in refused)} are not "
                "available in public-project conversations: only the "
                "built-in system skills listed in the System Skills section "
                "can be loaded here."
            ),
            "available_skills": sorted(PUBLIC_SYSTEM_SKILLS),
        })
    return await _handle_load_skills(
        user["id"], skill_ids,
        connected_services=get_public_connected_services(user),
        has_project=True, base_url="", api_key="",
        conversation_id=conversation_id, user=user,
    )


async def _build_autoload_resolver(user_id: int, project_id: str | None = None):
    """Build a per-call resolver mapping a skill id to its autoload tiers.

    Computes the three autoload membership sets once (user tier, project
    tier, and a per-routine map for the project's routines) so each skill in
    a listing/detail can be flagged without re-querying. Routine-tier autoloads
    are enumerated across *all* of the project's routines (by name) since
    ``routine_id`` is not threaded through tool dispatch -- see devplan 00120
    D3 Option A.

    Args:
        user_id: User's integer ID (user tier).
        project_id: Conversation's project ID, or None (project/routine tiers
            only computed when present).

    Returns:
        A callable ``autoload_for(skill_id) -> dict`` returning
        ``{"user": bool, "project": bool, "routines": [routine_name, ...]}``.
    """
    from db.skill_store import (
        list_user_autoloaded_skill_ids,
        list_project_autoloaded_skill_ids,
        list_routine_autoloaded_skill_ids,
    )

    user_set = set(await list_user_autoloaded_skill_ids(user_id))

    project_set: set[str] = set()
    routine_sets: list[tuple[str, set[str]]] = []
    if project_id:
        project_set = set(await list_project_autoloaded_skill_ids(project_id))
        from db.routine_store import list_routines
        routines = await list_routines(project_id)
        for routine in routines:
            ids = set(await list_routine_autoloaded_skill_ids(routine["id"]))
            routine_sets.append((routine["name"], ids))

    def autoload_for(skill_id: str) -> dict:
        return {
            "user": skill_id in user_set,
            "project": skill_id in project_set,
            "routines": [name for name, ids in routine_sets if skill_id in ids],
        }

    return autoload_for


async def _handle_list_my_skills(
    user_id: int,
    project_id: str | None = None,
    exclude_own: bool = False,
    exclude_shared: bool = False,
    exclude_public: bool = False,
    exclude_project: bool = False,
) -> str:
    """List the skills the user can see (own + shared + public + project), categorized.

    Read-only inspection tool. Returns compact metadata only (no skill
    ``content`` bodies and no ``system:*`` skills); each entry carries its
    category and inline autoload status across the user/project/routine tiers.
    When the conversation belongs to a project, skills scoped to that project
    (visibility 'project') are also included under the "project" category.

    Args:
        user_id: User's integer ID.
        project_id: Conversation's project ID, or None (drives project/routine
            autoload tiers and inclusion of project-scoped skills).
        exclude_own: Drop skills the user authored.
        exclude_shared: Drop skills shared directly with the user.
        exclude_public: Drop public skills the user did not author.
        exclude_project: Drop skills scoped to the current project.

    Returns:
        JSON string ``{"skill_count": N, "skills": [...]}``.
    """
    try:
        from db.skill_store import list_accessible_skills, list_project_skills

        skills = await list_accessible_skills(user_id)
        # Project-scoped skills (visibility 'project') are excluded from
        # list_accessible_skills; pull them in separately when the
        # conversation is in a project so they appear in the listing.
        if project_id and not exclude_project:
            skills = skills + await list_project_skills(project_id)
        autoload_for = await _build_autoload_resolver(user_id, project_id)

        payload: list[dict] = []
        seen_ids: set[str] = set()
        for s in skills:
            if s["id"] in seen_ids:
                continue
            seen_ids.add(s["id"])

            if s["visibility"] == "project":
                category = "project"
            elif s["creator_id"] == user_id:
                category = "own"
            elif s["visibility"] == "public":
                category = "public"
            else:
                category = "shared"

            if category == "own" and exclude_own:
                continue
            if category == "shared" and exclude_shared:
                continue
            if category == "public" and exclude_public:
                continue
            if category == "project" and exclude_project:
                continue

            payload.append({
                "id": s["id"],
                "name": s["name"],
                "description": s["description"],
                "visibility": s["visibility"],
                "category": category,
                "creator_name": s["creator_name"],
                "creator_email": s["creator_email"],
                "autoload": autoload_for(s["id"]),
            })

        return json.dumps({
            "skill_count": len(payload),
            "skills": payload,
        })
    except Exception as e:
        return json.dumps({"error": f"Failed to list skills: {e}"})


async def _handle_get_skill(
    user_id: int,
    skill_id: str,
    project_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Return the full contents and settings of a single skill by id.

    Read-only inspection tool. Gates the body behind
    ``user_can_access_skill`` and only fetches the share roster (which has no
    access check of its own) when the caller is the skill's creator.

    Args:
        user_id: User's integer ID.
        skill_id: Skill UUID to fetch.
        project_id: Conversation's project ID, or None (drives autoload tiers).
        conversation_id: Conversation UUID (records the read for the
            edit_skill content-edit read gate).

    Returns:
        JSON string ``{"skill": {...}}`` or ``{"error": ...}``.
    """
    if not skill_id:
        return json.dumps({"error": "Please provide a skill_id."})

    try:
        from db.skill_store import (
            user_can_access_skill,
            get_skill,
            get_project_skill,
            list_skill_shares,
        )

        # user_can_access_skill only covers own/public/shared visibility.
        # A project-scoped skill (creator_id None, visibility 'project') is
        # accessible when it belongs to this conversation's project.
        can_access = await user_can_access_skill(user_id, skill_id)
        if not can_access and project_id:
            can_access = await get_project_skill(project_id, skill_id) is not None
        if not can_access:
            return json.dumps({"error": "Skill not found or not accessible."})

        skill = await get_skill(skill_id)
        if skill is None:
            return json.dumps({"error": "Skill not found or not accessible."})

        is_owner = skill["creator_id"] == user_id

        shares = None
        if is_owner:
            raw_shares = await list_skill_shares(skill_id)
            shares = [
                {"user_id": sh["user_id"], "email": sh["email"], "name": sh["name"]}
                for sh in raw_shares
            ]

        autoload_for = await _build_autoload_resolver(user_id, project_id)
        _mark_skills_read(conversation_id, [skill["id"]])

        return json.dumps({
            "skill": {
                "id": skill["id"],
                "name": skill["name"],
                "description": skill["description"],
                "content": skill["content"],
                "visibility": skill["visibility"],
                "creator_id": skill["creator_id"],
                "creator_name": skill["creator_name"],
                "creator_email": skill["creator_email"],
                "created_at": skill["created_at"],
                "updated_at": skill["updated_at"],
                "is_owner": is_owner,
                "shares": shares,
                "autoload": autoload_for(skill_id),
            }
        })
    except Exception as e:
        return json.dumps({"error": f"Failed to get skill: {e}"})

