"""add project_doc_sources link table

Revision ID: b4d7e2a9c6f1
Revises: a9c2e7f4b1d3
Create Date: 2026-10-07 12:00:00.000000

Creates ``project_doc_sources``: one row per private project that was
given read access to the Quest Docs of one public project (Project
Settings > Docs access). Conversations of ``project_id`` may read, never
write, the docs of ``source_project_id``; both FKs cascade with their
project. Empty on upgrade: no project has access to any other project's
docs until its owner configures it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4d7e2a9c6f1'
down_revision: Union[str, Sequence[str], None] = 'a9c2e7f4b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the project_doc_sources link table."""
    op.create_table(
        'project_doc_sources',
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.Column('source_project_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['source_project_id'], ['projects.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('project_id', 'source_project_id'),
    )
    op.create_index(
        'ix_project_doc_sources_source_project_id',
        'project_doc_sources', ['source_project_id'],
    )


def downgrade() -> None:
    """Drop the project_doc_sources link table."""
    op.drop_index(
        'ix_project_doc_sources_source_project_id', table_name='project_doc_sources',
    )
    op.drop_table('project_doc_sources')
