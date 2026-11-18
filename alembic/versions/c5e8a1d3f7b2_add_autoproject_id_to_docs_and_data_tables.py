"""add autoproject_id to docs and data_tables

Revision ID: c5e8a1d3f7b2
Revises: 4b57cca1158c
Create Date: 2026-11-18 12:00:00.000000

AutoProject docs and tables: a Quest Doc or Quest Table can belong to an
AutoProject the way it can belong to a project. ``docs.autoproject_id`` and
``data_tables.autoproject_id`` are NULL for every user and project artifact
and name the owning AutoProject otherwise; a row never carries both a
``project_id`` and an ``autoproject_id`` (the stores refuse it). Both keys
cascade, so deleting an AutoProject deletes the rows of its docs and tables
(the service sweeps their directories).

SQLite cannot add a constraint with ALTER TABLE, and a batch (copy and move)
rebuild of ``docs`` or ``data_tables`` would drop the table under ``PRAGMA
foreign_keys=ON`` (alembic/env.py) and cascade away every row referencing
it (shares, doc memories, note suggestions). So both columns are added
with a raw ``ADD COLUMN ... REFERENCES``, which SQLite allows for a column
whose default is NULL; ``alembic/env.py`` (``_INLINE_FKS``) keeps
autogenerate from proposing to re-create the keys.

Existing rows need no backfill: no doc or table belongs to an AutoProject.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c5e8a1d3f7b2'
down_revision: Union[str, Sequence[str], None] = '4b57cca1158c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLES = ('docs', 'data_tables')


def upgrade() -> None:
    """Add the nullable, cascading autoproject_id to docs and data_tables."""
    for table in _TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN autoproject_id VARCHAR(36) "
            "REFERENCES autoprojects(id) ON DELETE CASCADE"
        )
        op.create_index(f'ix_{table}_autoproject_id', table, ['autoproject_id'])


def downgrade() -> None:
    """Drop autoproject_id from docs and data_tables.

    The AutoProject docs and tables are deleted first: without the column
    they would turn into ordinary user docs and tables that older code lists
    and writes from every chat. Their directories under ``DOCS_DIR`` and
    ``TABLES_DIR`` stay on disk. The column goes with a native ``ALTER
    TABLE DROP COLUMN``, no table rebuild.
    """
    for table in _TABLES:
        op.execute(f"DELETE FROM {table} WHERE autoproject_id IS NOT NULL")
        op.drop_index(f'ix_{table}_autoproject_id', table_name=table)
        op.drop_column(table, 'autoproject_id')
