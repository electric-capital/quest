"""add autoprojects, autoproject_runs, autoproject_brief_versions and conversations.autoproject_id

Revision ID: 4b57cca1158c
Revises: a3d9e5c7f2b4
Create Date: 2026-11-10 12:00:00.000000

AutoProjects: an ``autoprojects`` row holds an AutoProject's title, the brief
(with its version counter) and the counters the AutoProject page and its
unread dot run on. Each prompt is answered by a run, an ``autoproject_runs``
row numbered per AutoProject by ``seq`` and tied one-to-one to its own
conversation. The run row also freezes the run's starting ``## AutoProject``
context block with the brief version it shows (``context``,
``context_brief_version``, NULL until the run's first turn), so a resumed run
rebuilds the prompt it started with, and records ``brief_conflict_version``,
the brief version a ``brief_conflict`` result handed the run.
``autoproject_brief_versions`` keeps the recent brief texts. A run's
conversation carries ``conversations.autoproject_id``.

SQLite cannot add a constraint with ALTER TABLE, and a batch (copy and move)
rebuild of ``conversations`` would drop the table under ``PRAGMA
foreign_keys=ON`` (alembic/env.py) and cascade away every row referencing
it (memory suggestions, Slack thread mappings, doc note suggestions, ...).
So ``autoproject_id`` is added with a raw ``ADD COLUMN ... REFERENCES``, which
SQLite allows for a column whose default is NULL; ``alembic/env.py``
(``_INLINE_FKS``) keeps autogenerate from proposing to re-create the key,
whose ON DELETE SQLAlchemy's reflection cannot read from an inline clause.

Existing rows need no backfill: no conversation is an AutoProject run.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4b57cca1158c'
down_revision: Union[str, Sequence[str], None] = 'a3d9e5c7f2b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the three AutoProject tables and add conversations.autoproject_id."""
    op.create_table(
        'autoprojects',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column(
            'title_is_default', sa.Boolean(), nullable=False,
            server_default=sa.true(),
        ),
        sa.Column('brief', sa.Text(), nullable=False, server_default=''),
        sa.Column(
            'brief_version', sa.Integer(), nullable=False, server_default='0',
        ),
        sa.Column('brief_updated_at', sa.DateTime(), nullable=True),
        sa.Column('model', sa.String(length=100), nullable=True),
        sa.Column('thinking_effort', sa.String(length=20), nullable=True),
        sa.Column(
            'archived', sa.Boolean(), nullable=False, server_default=sa.false(),
        ),
        sa.Column(
            'next_run_seq', sa.Integer(), nullable=False, server_default='1',
        ),
        sa.Column('event_seq', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('unread_seq', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('read_seq', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('last_activity_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_autoprojects_user_id_archived_last_activity', 'autoprojects',
        ['user_id', 'archived', 'last_activity_at'],
    )

    # Inline foreign key: see the module docstring. Never batch-rebuild
    # ``conversations``.
    op.execute(
        "ALTER TABLE conversations ADD COLUMN autoproject_id VARCHAR(36) "
        "REFERENCES autoprojects(id) ON DELETE CASCADE"
    )
    op.create_index('ix_conversations_autoproject_id', 'conversations', ['autoproject_id'])

    op.create_table(
        'autoproject_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('autoproject_id', sa.String(length=36), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('conversation_id', sa.String(length=36), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column(
            'handoff_missing', sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('model', sa.String(length=100), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('context', sa.Text(), nullable=True),
        sa.Column('context_brief_version', sa.Integer(), nullable=True),
        sa.Column('brief_conflict_version', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['autoproject_id'], ['autoprojects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['conversation_id'], ['conversations.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_autoproject_runs_autoproject_id_seq', 'autoproject_runs',
        ['autoproject_id', 'seq'], unique=True,
    )
    op.create_index(
        'ix_autoproject_runs_autoproject_id_status', 'autoproject_runs',
        ['autoproject_id', 'status'],
    )
    op.create_index(
        'ix_autoproject_runs_conversation_id', 'autoproject_runs', ['conversation_id'],
        unique=True,
    )

    op.create_table(
        'autoproject_brief_versions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('autoproject_id', sa.String(length=36), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['autoproject_id'], ['autoprojects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_autoproject_brief_versions_autoproject_id_version', 'autoproject_brief_versions',
        ['autoproject_id', 'version'], unique=True,
    )


def downgrade() -> None:
    """Drop the AutoProject tables and conversations.autoproject_id (all AutoProjects are lost).

    The run conversations are deleted first (their dependent rows cascade):
    without ``autoproject_id`` they would turn into ordinary chats the older code
    lists in the sidebar. Their ``CHATS_DIR`` directories, and ``AUTOPROJECTS_DIR``,
    stay on disk. ``autoproject_id`` goes with a native ``ALTER TABLE DROP
    COLUMN``, no table rebuild.
    """
    op.drop_index(
        'ix_autoproject_brief_versions_autoproject_id_version',
        table_name='autoproject_brief_versions',
    )
    op.drop_table('autoproject_brief_versions')

    op.drop_index('ix_autoproject_runs_conversation_id', table_name='autoproject_runs')
    op.drop_index('ix_autoproject_runs_autoproject_id_status', table_name='autoproject_runs')
    op.drop_index('ix_autoproject_runs_autoproject_id_seq', table_name='autoproject_runs')
    op.drop_table('autoproject_runs')

    op.execute("DELETE FROM conversations WHERE autoproject_id IS NOT NULL")
    op.drop_index('ix_conversations_autoproject_id', table_name='conversations')
    op.drop_column('conversations', 'autoproject_id')

    op.drop_index('ix_autoprojects_user_id_archived_last_activity', table_name='autoprojects')
    op.drop_table('autoprojects')
