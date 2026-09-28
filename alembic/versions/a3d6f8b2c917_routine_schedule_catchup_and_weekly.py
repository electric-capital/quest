"""routine schedules: weekly type, next_due_at, run ledger

Revision ID: a3d6f8b2c917
Revises: f7d2a9c41e58
Create Date: 2026-09-28 12:00:00.000000

Adds the ``weekly`` schedule type (``weekly_days``), the ``next_due_at``
column anchored schedules fire from (so a run due while the server was down
is caught up instead of missed), and the ``routine_schedule_runs`` ledger of
scheduled occurrences. Existing schedules keep ``next_due_at`` NULL; the
scheduler computes the next occurrence from "now" on its first poll, so the
upgrade itself never triggers a run.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3d6f8b2c917'
down_revision: Union[str, Sequence[str], None] = 'f7d2a9c41e58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'routine_schedules',
        sa.Column('weekly_days', sa.String(length=20), nullable=True),
    )
    op.add_column(
        'routine_schedules',
        sa.Column('next_due_at', sa.DateTime(), nullable=True),
    )
    op.create_table(
        'routine_schedule_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('schedule_id', sa.String(length=36), nullable=False),
        sa.Column('occurrence_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('attempt', sa.Integer(), server_default='0', nullable=False),
        sa.Column('conversation_id', sa.String(length=36), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['schedule_id'], ['routine_schedules.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_routine_schedule_runs_schedule_occurrence',
        'routine_schedule_runs', ['schedule_id', 'occurrence_at'], unique=True,
    )
    op.create_index(
        'ix_routine_schedule_runs_status',
        'routine_schedule_runs', ['status'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_routine_schedule_runs_status', table_name='routine_schedule_runs')
    op.drop_index(
        'ix_routine_schedule_runs_schedule_occurrence', table_name='routine_schedule_runs',
    )
    op.drop_table('routine_schedule_runs')
    op.drop_column('routine_schedules', 'next_due_at')
    op.drop_column('routine_schedules', 'weekly_days')
