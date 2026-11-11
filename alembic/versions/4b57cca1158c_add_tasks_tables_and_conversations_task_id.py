"""add tasks, task_runs, task_brief_versions and conversations.task_id

Revision ID: 4b57cca1158c
Revises: a3d9e5c7f2b4
Create Date: 2026-11-10 12:00:00.000000

Quest Tasks: a ``tasks`` row holds a task's title, the brief (with its
version counter) and the counters the thread and the unread dot run on. Each prompt is answered by a run, a
``task_runs`` row numbered per task by ``seq`` and tied one-to-one to its own
conversation; ``task_brief_versions`` keeps the recent brief texts. A run's
conversation carries ``conversations.task_id``.

SQLite cannot add a constraint with ALTER TABLE, and a batch (copy and move)
rebuild of ``conversations`` would drop the table under ``PRAGMA
foreign_keys=ON`` (alembic/env.py) and cascade away every row referencing
it (memory suggestions, Slack thread mappings, doc note suggestions, ...).
So ``task_id`` is added with a raw ``ADD COLUMN ... REFERENCES``, which
SQLite allows for a column whose default is NULL; ``alembic/env.py``
(``_INLINE_FKS``) keeps autogenerate from proposing to re-create the key,
whose ON DELETE SQLAlchemy's reflection cannot read from an inline clause.

Existing rows need no backfill: no conversation is a task run.
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
    """Create the three task tables and add conversations.task_id."""
    op.create_table(
        'tasks',
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
        'ix_tasks_user_id_archived_last_activity', 'tasks',
        ['user_id', 'archived', 'last_activity_at'],
    )

    # Inline foreign key: see the module docstring. Never batch-rebuild
    # ``conversations``.
    op.execute(
        "ALTER TABLE conversations ADD COLUMN task_id VARCHAR(36) "
        "REFERENCES tasks(id) ON DELETE CASCADE"
    )
    op.create_index('ix_conversations_task_id', 'conversations', ['task_id'])

    op.create_table(
        'task_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('task_id', sa.String(length=36), nullable=False),
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
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['conversation_id'], ['conversations.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_task_runs_task_id_seq', 'task_runs', ['task_id', 'seq'], unique=True,
    )
    op.create_index(
        'ix_task_runs_task_id_status', 'task_runs', ['task_id', 'status'],
    )
    op.create_index(
        'ix_task_runs_conversation_id', 'task_runs', ['conversation_id'],
        unique=True,
    )

    op.create_table(
        'task_brief_versions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('task_id', sa.String(length=36), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_task_brief_versions_task_id_version', 'task_brief_versions',
        ['task_id', 'version'], unique=True,
    )


def downgrade() -> None:
    """Drop the task tables and conversations.task_id (all tasks are lost).

    The run conversations are deleted first (their dependent rows cascade):
    without ``task_id`` they would turn into ordinary chats the older code
    lists in the sidebar. Their ``CHATS_DIR`` directories, and ``TASKS_DIR``,
    stay on disk. ``task_id`` goes with a native ``ALTER TABLE DROP
    COLUMN``, no table rebuild.
    """
    op.drop_index(
        'ix_task_brief_versions_task_id_version',
        table_name='task_brief_versions',
    )
    op.drop_table('task_brief_versions')

    op.drop_index('ix_task_runs_conversation_id', table_name='task_runs')
    op.drop_index('ix_task_runs_task_id_status', table_name='task_runs')
    op.drop_index('ix_task_runs_task_id_seq', table_name='task_runs')
    op.drop_table('task_runs')

    op.execute("DELETE FROM conversations WHERE task_id IS NOT NULL")
    op.drop_index('ix_conversations_task_id', table_name='conversations')
    op.drop_column('conversations', 'task_id')

    op.drop_index('ix_tasks_user_id_archived_last_activity', table_name='tasks')
    op.drop_table('tasks')
