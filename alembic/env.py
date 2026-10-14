import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool
from sqlalchemy import text

from alembic import context

# Add project root to path so we can import our models
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.models import Base
from config.paths import DATABASE_PATH, migrate_legacy_database_file

# Pick up a pre-rename praixy.db before migrating so manual `alembic
# upgrade` runs never create a fresh database beside the legacy one.
migrate_legacy_database_file()

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Override the database URL from the shared config module so that
# the data directory can be configured externally via server_config.json.
# The alembic.ini value serves as a fallback only.
config.set_main_option("sqlalchemy.url", f"sqlite:///{DATABASE_PATH}")

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target_metadata from our models for autogenerate support
target_metadata = Base.metadata

# ---- FTS5 table exclusion for autogenerate ----
# SQLite FTS5 virtual tables and their shadow tables (suffixed with
# _config, _data, _docsize, _idx) are created via raw SQL in Alembic
# migrations, not via SQLAlchemy ORM models.  Without this exclusion,
# autogenerate would propose dropping them on every run.
_FTS_VIRTUAL_TABLES = {"memories_fts", "project_memories_fts"}
_FTS_SHADOW_SUFFIXES = ("_config", "_data", "_docsize", "_idx")


def _is_fts_table(table_name: str) -> bool:
    """Return True if the table is an FTS5 virtual table or shadow table."""
    if table_name in _FTS_VIRTUAL_TABLES:
        return True
    for fts_name in _FTS_VIRTUAL_TABLES:
        for suffix in _FTS_SHADOW_SUFFIXES:
            if table_name == fts_name + suffix:
                return True
    return False


def include_name(name, type_, parent_names):
    """Filter out FTS5 virtual tables and their shadow tables from autogenerate."""
    if type_ == "table":
        return not _is_fts_table(name)
    return True


# ---- PK nullable suppression for autogenerate ----
# SQLite's PRAGMA table_info reports notnull=0 for PRIMARY KEY columns,
# but PKs are inherently NOT NULL.  Alembic detects this as a mismatch
# and proposes alter_column(nullable=False).  This hook strips those
# no-op directives so autogenerate produces a clean migration.

def _is_pk_nullable_op(op_obj, pk_columns):
    """Check if an AlterColumnOp is a no-op PK nullable change on SQLite."""
    from alembic.operations.ops import AlterColumnOp

    if not isinstance(op_obj, AlterColumnOp):
        return False

    table_name = op_obj.table_name
    col_name = op_obj.column_name

    if col_name not in pk_columns.get(table_name, set()):
        return False

    # Only filter if the op is purely a nullable change
    if op_obj.modify_nullable is None:
        return False
    if op_obj.modify_type is not None:
        return False

    return True


def _filter_pk_nullable_ops(ops_list, pk_columns):
    """Filter out PK nullable AlterColumnOps from an ops list."""
    from alembic.operations.ops import ModifyTableOps

    filtered = []
    for op in ops_list:
        if isinstance(op, ModifyTableOps):
            new_ops = [
                sub_op for sub_op in op.ops
                if not _is_pk_nullable_op(sub_op, pk_columns)
            ]
            if new_ops:
                op.ops = new_ops
                filtered.append(op)
        elif _is_pk_nullable_op(op, pk_columns):
            continue
        else:
            filtered.append(op)
    return filtered


def _process_revision_directives(context, revision, directives):
    """Remove no-op alter_column operations for PK nullable on SQLite."""
    if not directives:
        return

    script = directives[0]

    # Collect PK column names per table from our metadata
    pk_columns = {}
    for table_name, table in target_metadata.tables.items():
        pk_columns[table_name] = {col.name for col in table.primary_key.columns}

    # Filter upgrade ops
    if script.upgrade_ops and script.upgrade_ops.ops:
        script.upgrade_ops.ops = _filter_pk_nullable_ops(
            script.upgrade_ops.ops, pk_columns
        )

    # Filter downgrade ops
    if script.downgrade_ops and script.downgrade_ops.ops:
        script.downgrade_ops.ops = _filter_pk_nullable_ops(
            script.downgrade_ops.ops, pk_columns
        )


# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
        process_revision_directives=_process_revision_directives,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.begin() as connection:
        # Enable SQLite foreign key enforcement for this connection so that
        # any DELETE operations within migrations respect CASCADE constraints.
        connection.execute(text("PRAGMA foreign_keys=ON"))

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=include_name,
            process_revision_directives=_process_revision_directives,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
