"""Shared support for the end-to-end Alembic migration tests.

The ``tests/test_*_migration.py`` files build a database at some past
revision in a throwaway data directory, seed rows the way that release
would have written them, upgrade, and read the result back both in raw SQL
and through the ORM stores. Everything runs **in this process**: the
``deployment`` fixture below swaps the process into a prod-mode deployment
whose data dir, key file and encryption password live under ``tmp_path``
(env vars plus the already-imported ``config.paths`` constants), and
``Deployment.alembic()`` drives ``alembic.command`` against the repo's
``alembic/`` script directory. ``alembic/env.py`` is re-executed on every
command and reads ``config.paths.DATABASE_PATH`` at that moment, so the
patched path is what the migration opens -- exactly the file a
deployment's ``alembic upgrade head`` would.

The ORM stores import ``db.engine.AsyncSessionLocal`` at import time, so
``deployment`` rebinds that name on each store module in
``ORM_STORE_MODULES`` to a session factory over the temp database.

A failing migration surfaces as the exception the revision raised
(``RuntimeError`` for the encryption-key guards); nothing is committed,
matching the non-zero exit a CLI run would report.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config import encryption, paths
from config.paths import PROJECT_ROOT

PASSWORD = "migration-test-password"

# Store modules whose module-level ``AsyncSessionLocal`` the fixture points
# at the temp database (the read-back path of every migration test).
ORM_STORE_MODULES = ("db.user_store", "db.user_service_credential_store")


def alembic_config() -> Config:
    """An Alembic config for the repo's script directory.

    Built without ``alembic.ini`` on purpose: the ini's ``[loggers]``
    section would make ``env.py`` call ``logging.config.fileConfig`` and
    disable every logger the test process already has. ``env.py`` sets
    ``sqlalchemy.url`` itself from ``config.paths.DATABASE_PATH``.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


def rows(db_path: Path, sql: str, params=()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def execute(db_path: Path, sql: str, params=()) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def columns(db_path: Path, table: str) -> set[str]:
    return {r[1] for r in rows(db_path, f"PRAGMA table_info({table})")}


def metadata_drift(db_path: Path) -> list[str]:
    """``repr`` of every difference between the ORM metadata and the
    database at ``db_path`` (what ``alembic revision --autogenerate`` would
    emit). compare_metadata does NOT compare sqlite_where or server
    defaults, so callers assert those from the index SQL / rows directly.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from db.models import Base

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    finally:
        engine.dispose()
    return [repr(d) for d in diffs]


def run(coro):
    return asyncio.run(coro)


class Deployment:
    """A prod-mode deployment rooted at a temp data dir (see ``deployment``)."""

    def __init__(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch):
        self.data_dir = data_dir
        self.db_path = data_dir / "quest.db"
        self.key_file = data_dir / "encryption_key.json"
        self.password = PASSWORD
        self._monkeypatch = monkeypatch
        self._async_engine = None

    # -- Alembic -----------------------------------------------------------

    def alembic(self, verb: str, revision: str) -> None:
        """``alembic <verb> <revision>`` for upgrade / downgrade / stamp."""
        getattr(command, verb)(alembic_config(), revision)

    # -- Environment -------------------------------------------------------

    @contextmanager
    def env(self, **overrides: str | None):
        """Run a block as a process started with these env vars changed
        (``None`` unsets one), with the encryption key cache emptied on the
        way in and out so the password in force is the one that gets checked.
        """
        with pytest.MonkeyPatch.context() as mp:
            for name, value in overrides.items():
                if value is None:
                    mp.delenv(name, raising=False)
                else:
                    mp.setenv(name, value)
            encryption.reset_cache()
            try:
                yield
            finally:
                encryption.reset_cache()

    # -- ORM ---------------------------------------------------------------

    def bind_orm(self, modules) -> None:
        """Point each store module's ``AsyncSessionLocal`` at the temp db."""
        self._async_engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.db_path}", poolclass=NullPool,
        )
        factory = async_sessionmaker(self._async_engine, expire_on_commit=False)
        for module in modules:
            self._monkeypatch.setattr(module, "AsyncSessionLocal", factory, raising=True)

    def dispose(self) -> None:
        if self._async_engine is not None:
            run(self._async_engine.dispose())
            self._async_engine = None


@pytest.fixture()
def deployment(tmp_path, monkeypatch):
    """The process as a prod deployment whose data dir is under ``tmp_path``.

    Sets ``QUEST_ENV=prod``, ``QUEST_DATA_DIR``, ``QUEST_ENCRYPTION_KEY_FILE``
    and ``QUEST_ENCRYPTION_PASSWORD`` (and unsets the password file), points
    ``config.paths.DATA_DIR`` / ``DATABASE_PATH`` at it, rebinds the ORM
    stores, and empties the encryption key cache so the first secret
    touched unwraps *this* deployment's key file. The key file itself is
    not created: tests that need one call ``encryption.create_key_file``
    (or exercise the ``init`` CLI).

    Teardown restores the env, the paths and the store bindings, then
    reloads the test session's own key (the repo-root conftest.py's) so
    later tests see the cache they started with.
    """
    # Import (and so bind db.engine to) the real database before the paths
    # are patched: a first import of db.engine during the test would pin the
    # process-wide engine to this temp file for the rest of the session.
    stores = [importlib.import_module(name) for name in ORM_STORE_MODULES]

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    dep = Deployment(data_dir, monkeypatch)

    monkeypatch.setenv("QUEST_ENV", "prod")
    monkeypatch.setenv("QUEST_DATA_DIR", str(data_dir))
    monkeypatch.setenv("QUEST_ENCRYPTION_KEY_FILE", str(dep.key_file))
    monkeypatch.setenv("QUEST_ENCRYPTION_PASSWORD", PASSWORD)
    monkeypatch.delenv("QUEST_ENCRYPTION_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(paths, "DATA_DIR", data_dir, raising=True)
    monkeypatch.setattr(paths, "DATABASE_PATH", dep.db_path, raising=True)
    dep.bind_orm(stores)
    encryption.reset_cache()
    try:
        yield dep
    finally:
        dep.dispose()
        monkeypatch.undo()
        encryption.reset_cache()
        encryption.load_data_key(create=True)
