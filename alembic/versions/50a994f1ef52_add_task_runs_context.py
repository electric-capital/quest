"""add task_runs.context and task_runs.context_brief_version

Revision ID: 50a994f1ef52
Revises: 4b57cca1158c
Create Date: 2026-11-10 18:00:00.000000

A task run's system prompt carries a ``## Task`` block built from the task's
instructions, brief and log. The block is frozen on the run row on the run's
first turn, with the brief version it shows, so a run resumed after a server
restart (a question answered days later) rebuilds the prompt it started with
rather than one built from the task as it now is.

Existing rows need no backfill: NULL means "not frozen yet", and a run with
no frozen context freezes one on its next turn.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '50a994f1ef52'
down_revision: Union[str, Sequence[str], None] = '4b57cca1158c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the frozen run context and its brief version to task_runs."""
    op.add_column('task_runs', sa.Column('context', sa.Text(), nullable=True))
    op.add_column(
        'task_runs',
        sa.Column('context_brief_version', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Remove the frozen run context columns from task_runs."""
    with op.batch_alter_table('task_runs', schema=None) as batch_op:
        batch_op.drop_column('context_brief_version')
        batch_op.drop_column('context')
