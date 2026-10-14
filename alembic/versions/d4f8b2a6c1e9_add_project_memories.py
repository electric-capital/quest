"""add project_memories and project_memory_suggestions

Revision ID: d4f8b2a6c1e9
Revises: e5c97b362821
Create Date: 2026-10-14 18:00:00.000000

Project memories are the project-scoped twin of ``memories``: one row per
fact attached to a project (cascading with it), searchable through the
``project_memories_fts`` FTS5 table kept in sync by triggers exactly like
``memories_fts``. ``project_memory_suggestions`` holds what the model
proposed through the non-blocking ``suggest_project_memory`` tool, with the
same pending / accepted / dismissed lifecycle as ``memory_suggestions``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4f8b2a6c1e9'
down_revision: Union[str, Sequence[str], None] = 'e5c97b362821'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create both tables, their indexes, the FTS5 table and its triggers."""
    op.create_table(
        'project_memories',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('archived', sa.Boolean(), server_default='0', nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_project_memories_project_id_archived', 'project_memories',
        ['project_id', 'archived'],
    )

    op.execute("""
        CREATE VIRTUAL TABLE project_memories_fts USING fts5(
            content,
            content='project_memories',
            content_rowid='rowid'
        );
    """)
    op.execute("""
        CREATE TRIGGER project_memories_ai AFTER INSERT ON project_memories BEGIN
            INSERT INTO project_memories_fts(rowid, content)
            VALUES (NEW.rowid, NEW.content);
        END;
    """)
    op.execute("""
        CREATE TRIGGER project_memories_ad AFTER DELETE ON project_memories BEGIN
            INSERT INTO project_memories_fts(project_memories_fts, rowid, content)
            VALUES ('delete', OLD.rowid, OLD.content);
        END;
    """)
    op.execute("""
        CREATE TRIGGER project_memories_au AFTER UPDATE ON project_memories BEGIN
            INSERT INTO project_memories_fts(project_memories_fts, rowid, content)
            VALUES ('delete', OLD.rowid, OLD.content);
            INSERT INTO project_memories_fts(rowid, content)
            VALUES (NEW.rowid, NEW.content);
        END;
    """)

    op.create_table(
        'project_memory_suggestions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.Column('conversation_id', sa.String(length=36), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('replaces_memory_id', sa.String(length=36), nullable=True),
        sa.Column(
            'status', sa.String(length=20), nullable=False,
            server_default='pending',
        ),
        sa.Column('memory_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['conversation_id'], ['conversations.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['replaces_memory_id'], ['project_memories.id'], ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['memory_id'], ['project_memories.id'], ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_project_memory_suggestions_user_id', 'project_memory_suggestions',
        ['user_id'],
    )
    op.create_index(
        'ix_project_memory_suggestions_conversation_status',
        'project_memory_suggestions', ['conversation_id', 'status'],
    )


def downgrade() -> None:
    """Drop both tables (project memories and pending suggestions are lost)."""
    op.drop_index(
        'ix_project_memory_suggestions_conversation_status',
        table_name='project_memory_suggestions',
    )
    op.drop_index(
        'ix_project_memory_suggestions_user_id',
        table_name='project_memory_suggestions',
    )
    op.drop_table('project_memory_suggestions')

    op.execute("DROP TRIGGER IF EXISTS project_memories_au;")
    op.execute("DROP TRIGGER IF EXISTS project_memories_ad;")
    op.execute("DROP TRIGGER IF EXISTS project_memories_ai;")
    op.execute("DROP TABLE IF EXISTS project_memories_fts;")
    op.drop_index(
        'ix_project_memories_project_id_archived', table_name='project_memories',
    )
    op.drop_table('project_memories')
