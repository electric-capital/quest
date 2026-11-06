"""convert guides to skills, add user_notifications, drop the guides schema

Revision ID: c5a9e3f7d2b1
Revises: b4e8d2c7a1f3
Create Date: 2026-11-06 12:00:00.000000

Guides (named system-prompt presets, one table plus ``routines.guide_id``)
were deprecated in favor of skills. This revision retires them for good:

1. ``user_notifications`` is created: the inbox's Other tab, system
   messages a user reads and dismisses.
2. Every guide with content becomes a private skill of its owner with the
   same content, and each owner gets one ``guides_converted`` notification
   naming the skills (and the routines that had a guide attached). No
   skill is auto-loaded, for the user or for a routine: the notification
   says so and where to turn it on. Guides without content (the default
   guide every user was given, never written to) are dropped silently.
3. ``routines.guide_id`` and the ``guides`` table are dropped.

Skill names are unique per creator, so a guide whose name is taken becomes
``<name> (converted)``, then ``(converted 2)`` and so on. The default guide
(always named "Default") becomes the skill "Default guide": it no longer
applies by default, and a skill just called "Default" would suggest it does.

Dropping ``routines.guide_id`` needs care on SQLite. The column carries a
table-level foreign key, which ``ALTER TABLE ... DROP COLUMN`` refuses, and
the usual answer, a batch (copy and move) rebuild of ``routines``, would
drop the old table under ``PRAGMA foreign_keys=ON`` (alembic/env.py) and
cascade away every routine schedule and routine skill auto-load. The pragma
cannot be turned off here either: it is a no-op inside a transaction, and
an earlier revision of the same ``alembic upgrade`` run has usually opened
one. So the foreign key clause is removed from the stored ``CREATE TABLE``
text instead (``PRAGMA writable_schema``, the procedure sqlite.org gives
for removing a constraint, which changes no stored row), after which the
native ``DROP COLUMN`` applies. ``routines`` must never be rebuilt with
batch mode.

The downgrade restores the empty schema only: the skills stay skills.
"""
import re
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5a9e3f7d2b1'
down_revision: Union[str, Sequence[str], None] = 'b4e8d2c7a1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Application-layer caps of db/skill_store.py at this revision.
_MAX_SKILL_NAME_LENGTH = 100

_NOTIFICATION_KIND = "guides_converted"
_NOTIFICATION_TITLE = "Your guides were converted to skills"

_skills = sa.table(
    'skills',
    sa.column('id', sa.String),
    sa.column('creator_id', sa.Integer),
    sa.column('project_id', sa.String),
    sa.column('name', sa.String),
    sa.column('description', sa.String),
    sa.column('content', sa.Text),
    sa.column('visibility', sa.String),
    sa.column('created_at', sa.DateTime),
    sa.column('updated_at', sa.DateTime),
)

_notifications = sa.table(
    'user_notifications',
    sa.column('id', sa.String),
    sa.column('user_id', sa.Integer),
    sa.column('kind', sa.String),
    sa.column('title', sa.String),
    sa.column('body', sa.Text),
    sa.column('data', sa.JSON),
    sa.column('created_at', sa.DateTime),
    sa.column('dismissed_at', sa.DateTime),
)

# The table-level clause 6766d7c126ba created, as SQLAlchemy rendered it
# (optionally named, optionally quoted).
_GUIDE_FK_CLAUSE = re.compile(
    r',\s*(?:CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\s*\(\s*["`\[]?guide_id["`\]]?\s*\)'
    r'\s*REFERENCES\s+["`\[]?guides["`\]]?\s*\(\s*["`\[]?id["`\]]?\s*\)'
    r'(?:\s+ON\s+DELETE\s+SET\s+NULL)?',
    re.IGNORECASE,
)


def _skill_name(guide_name: str, is_default: bool, taken: set[str]) -> str:
    """A skill name for the guide that its owner does not use yet."""
    base = ("Default guide" if is_default else guide_name.strip())
    base = base[:_MAX_SKILL_NAME_LENGTH]
    if base not in taken:
        return base
    for i in range(1, 1000):
        suffix = " (converted)" if i == 1 else f" (converted {i})"
        name = base[:_MAX_SKILL_NAME_LENGTH - len(suffix)].rstrip() + suffix
        if name not in taken:
            return name
    raise RuntimeError(f"No free skill name for guide {guide_name!r}")


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _notification_body(skills: list[dict], routines: list[dict]) -> str:
    lines = [
        "Guides have been retired in favor of skills. "
        + ("Your guide is now a private skill" if len(skills) == 1
           else f"Your {len(skills)} guides are now private skills")
        + " with the same content:",
        "",
    ]
    for skill in skills:
        if skill["skill_name"] == skill["guide_name"]:
            lines.append(f"- {skill['skill_name']}")
        else:
            lines.append(
                f"- {skill['guide_name']}, now the skill \"{skill['skill_name']}\""
            )
    lines += [
        "",
        "Guides no longer apply to any conversation, and the converted "
        "skills are not loaded automatically. To keep using one, turn on "
        "auto-load for it in Settings > Skills, or load it in the "
        "conversations where you want it.",
    ]
    if routines:
        lines += [
            "",
            _plural(len(routines), "routine") + " had a guide attached and now "
            + ("runs" if len(routines) == 1 else "run") + " without it. Add the "
            "skill to the routine's auto-loaded skills to keep its instructions:",
            "",
        ]
        for routine in routines:
            lines.append(
                f"- {routine['routine_name']} in {routine['project_name']} "
                f"(skill \"{routine['skill_name']}\")"
            )
    return "\n".join(lines)


def _convert_guides(bind) -> None:
    """One private skill per guide with content, one notification per owner."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    guides = bind.execute(sa.text(
        "SELECT id, user_id, name, content, is_default FROM guides "
        "WHERE trim(content, ' ' || char(9) || char(10) || char(13)) != '' "
        "ORDER BY user_id, is_default DESC, name, id"
    )).fetchall()

    taken_by_user: dict[int, set[str]] = {}
    skills_by_user: dict[int, list[dict]] = {}
    skill_by_guide: dict[str, dict] = {}
    owner_by_guide: dict[str, int] = {}
    for guide_id, user_id, name, content, is_default in guides:
        taken = taken_by_user.get(user_id)
        if taken is None:
            taken = {
                row[0] for row in bind.execute(
                    sa.text("SELECT name FROM skills WHERE creator_id = :uid"),
                    {"uid": user_id},
                )
            }
            taken_by_user[user_id] = taken
        skill_name = _skill_name(name, bool(is_default), taken)
        taken.add(skill_name)
        skill_id = str(uuid.uuid4())
        bind.execute(sa.insert(_skills).values(
            id=skill_id,
            creator_id=user_id,
            project_id=None,
            name=skill_name,
            description=f"Converted from the '{name}' guide."[:500],
            content=content,
            visibility="private",
            created_at=now,
            updated_at=None,
        ))
        entry = {
            "skill_id": skill_id,
            "skill_name": skill_name,
            "guide_name": name,
            "was_default": bool(is_default),
        }
        skills_by_user.setdefault(user_id, []).append(entry)
        skill_by_guide[guide_id] = entry
        owner_by_guide[guide_id] = user_id

    # Routines that carried a converted guide as an override. A routine's
    # guide was always one of its owner's own, so the routine is reported to
    # the guide's owner.
    routines_by_user: dict[int, list[dict]] = {}
    for routine_id, routine_name, guide_id, project_id, project_name in bind.execute(sa.text(
        "SELECT r.id, r.name, r.guide_id, r.project_id, p.name "
        "FROM routines r JOIN projects p ON p.id = r.project_id "
        "WHERE r.guide_id IS NOT NULL ORDER BY p.name, r.name, r.id"
    )):
        skill = skill_by_guide.get(guide_id)
        if skill is None:
            continue
        routines_by_user.setdefault(owner_by_guide[guide_id], []).append({
            "routine_id": routine_id,
            "routine_name": routine_name,
            "project_id": project_id,
            "project_name": project_name,
            "skill_id": skill["skill_id"],
            "skill_name": skill["skill_name"],
        })

    for user_id, skills in skills_by_user.items():
        routines = routines_by_user.get(user_id, [])
        bind.execute(sa.insert(_notifications).values(
            id=str(uuid.uuid4()),
            user_id=user_id,
            kind=_NOTIFICATION_KIND,
            title=_NOTIFICATION_TITLE,
            body=_notification_body(skills, routines),
            data={"skills": skills, "routines": routines},
            created_at=now,
            dismissed_at=None,
        ))


def _drop_routines_guide_fk(bind) -> None:
    """Remove the table-level ``guide_id`` foreign key clause from the
    stored ``CREATE TABLE routines`` text (see the module docstring)."""
    create_sql = bind.execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'routines'"
    )).scalar_one()
    stripped, removed = _GUIDE_FK_CLAUSE.subn('', create_sql)
    if removed == 0:
        # No table-level clause: the key is inline on the column (what this
        # revision's downgrade adds), and DROP COLUMN takes it along.
        return
    if removed > 1:
        raise RuntimeError(
            "Expected one guide_id foreign key in the routines table "
            f"definition, found {removed}: {create_sql!r}"
        )
    schema_version = bind.exec_driver_sql("PRAGMA schema_version").scalar_one()
    bind.exec_driver_sql("PRAGMA writable_schema=ON")
    try:
        bind.execute(
            sa.text(
                "UPDATE sqlite_master SET sql = :sql "
                "WHERE type = 'table' AND name = 'routines'"
            ),
            {"sql": stripped},
        )
        # Makes this and every other connection re-read the schema.
        bind.exec_driver_sql(f"PRAGMA schema_version={int(schema_version) + 1}")
    finally:
        bind.exec_driver_sql("PRAGMA writable_schema=OFF")
    referenced = {
        row[2] for row in bind.exec_driver_sql("PRAGMA foreign_key_list(routines)")
    }
    if "guides" in referenced:
        raise RuntimeError("routines still references guides after the schema edit")


def upgrade() -> None:
    """Create user_notifications, convert the guides, drop their schema."""
    op.create_table(
        'user_notifications',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=50), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('dismissed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_user_notifications_user_id', 'user_notifications', ['user_id'])
    op.create_index(
        'ix_user_notifications_user_created', 'user_notifications',
        ['user_id', 'created_at'],
    )

    bind = op.get_bind()
    _convert_guides(bind)

    _drop_routines_guide_fk(bind)
    op.drop_column('routines', 'guide_id')
    op.drop_index('ix_guides_user_id_name', table_name='guides')
    op.drop_index(op.f('ix_guides_user_id'), table_name='guides')
    op.drop_table('guides')


def downgrade() -> None:
    """Restore an empty guides table and ``routines.guide_id``, and drop
    user_notifications. The converted skills stay skills: nothing records
    which guide a skill came from once its notification is gone."""
    op.create_table(
        'guides',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('is_default', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_guides_user_id'), 'guides', ['user_id'], unique=False)
    op.create_index('ix_guides_user_id_name', 'guides', ['user_id', 'name'], unique=True)
    # Inline, so routines is not rebuilt (SQLite allows ADD COLUMN ...
    # REFERENCES for a column whose default is NULL).
    op.execute(
        "ALTER TABLE routines ADD COLUMN guide_id VARCHAR(36) "
        "REFERENCES guides(id) ON DELETE SET NULL"
    )

    op.drop_index('ix_user_notifications_user_created', table_name='user_notifications')
    op.drop_index('ix_user_notifications_user_id', table_name='user_notifications')
    op.drop_table('user_notifications')
