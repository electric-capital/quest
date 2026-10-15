"""Shared isolated-database fixture for store and route tests.

``isolated_db`` (exposed through tests/conftest.py; plugin suites can
import it from here into their own conftest) gives each test a fresh
SQLite file under ``tmp_path`` with the current ORM schema, built from
the already-imported ``db.models`` -- no module reloads. It installs the
same per-connection PRAGMAs as db/engine.py (foreign_keys=ON is what makes
ON DELETE CASCADE / SET NULL behave in tests) and monkeypatches the
session factories so every store module, and ``db.engine`` itself, talks
to the temp database for the duration of the test::

    @pytest.fixture()
    def _isolated_db(isolated_db):
        import db.action_request_store as store_mod
        return store_mod, isolated_db.models

    def test_something(_isolated_db):
        store_mod, models_mod = _isolated_db
        ...

Inside a test, ``from db.engine import AsyncSessionLocal`` and
``store_mod.AsyncSessionLocal`` both resolve to the test session factory,
exactly as they did when the modules were reloaded against a patched
``DATABASE_PATH``.

``SESSION_LOCAL_MODULES`` is the explicit list of modules that bind
``from db.engine import AsyncSessionLocal`` at import time and therefore
need their own copy patched. Keep it in sync with::

    grep -rl "from db.engine import" --include=*.py . | grep -v /tests/

A module that is first imported during a test (after the fixture ran)
binds the patched factory on its own; the fixture's teardown resets any
such binding back to the real one so nothing leaks into later tests.
"""

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

SESSION_LOCAL_MODULES = (
    "db.action_request_store",
    "db.conversation_store",
    "db.doc_store",
    "db.guide_store",
    "db.inference_api_key_store",
    "db.llm_call_store",
    "db.memory_store",
    "db.memory_suggestion_store",
    "db.password_store",
    "db.project_memory_store",
    "db.project_memory_suggestion_store",
    "db.project_store",
    "db.routine_store",
    "db.schedule_store",
    "db.skill_store",
    "db.table_store",
    "db.tool_wait_handle_store",
    "db.user_service_credential_store",
    "db.user_store",
    "db.user_subagent_run_store",
    "chat.slack_conversation_store",
)


def _set_sqlite_pragma(dbapi_connection, _connection_record):
    """Mirror db/engine.py's per-connection PRAGMAs on the test engines."""
    dbapi_connection.execute("PRAGMA foreign_keys=ON")
    dbapi_connection.execute("PRAGMA journal_mode=WAL")
    dbapi_connection.execute("PRAGMA busy_timeout=5000")
    dbapi_connection.execute("PRAGMA synchronous=NORMAL")


class IsolatedDb:
    """What ``isolated_db`` yields: the temp database and its engines."""

    def __init__(self, path: Path, engine, async_engine, session_local, monkeypatch):
        self.path = path
        #: Sync engine on the temp file (raw SQL inspection in tests).
        self.engine = engine
        self.async_engine = async_engine
        #: The async session factory every patched module now uses.
        self.AsyncSessionLocal = session_local
        self._monkeypatch = monkeypatch
        import db.models as models_mod

        #: The real, already-imported ``db.models`` module.
        self.models = models_mod

    def patch(self, *modules: "str | ModuleType") -> None:
        """Point more modules' ``AsyncSessionLocal`` at the temp database.

        For a module outside ``SESSION_LOCAL_MODULES`` that binds its own
        copy of the factory. Accepts module objects or dotted names.
        """
        for mod in modules:
            if isinstance(mod, str):
                mod = importlib.import_module(mod)
            self._monkeypatch.setattr(mod, "AsyncSessionLocal", self.AsyncSessionLocal)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Fresh SQLite database per test; see the module docstring."""
    import db.engine as engine_mod
    import db.models as models_mod
    from config import paths

    # Import the stores before patching db.engine: a module first imported
    # afterwards would bind the test factory as its "original" and
    # monkeypatch would restore it to that.
    store_modules = [importlib.import_module(name) for name in SESSION_LOCAL_MODULES]

    db_path = tmp_path / "quest.db"
    monkeypatch.setattr(paths, "DATABASE_PATH", db_path)

    engine = create_engine(
        f"sqlite:///{db_path}", echo=False, connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _set_sqlite_pragma)
    async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}", echo=False,
        pool_size=10, max_overflow=20, pool_timeout=30,
    )
    event.listen(async_engine.sync_engine, "connect", _set_sqlite_pragma)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)
    sync_session_local = sessionmaker(
        autocommit=False, autoflush=False, bind=engine,
    )

    models_mod.Base.metadata.create_all(engine)

    real_session_local = engine_mod.AsyncSessionLocal
    monkeypatch.setattr(engine_mod, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(engine_mod, "ASYNC_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setattr(engine_mod, "engine", engine)
    monkeypatch.setattr(engine_mod, "SessionLocal", sync_session_local)
    monkeypatch.setattr(engine_mod, "async_engine", async_engine)
    monkeypatch.setattr(engine_mod, "AsyncSessionLocal", session_local)
    for mod in store_modules:
        monkeypatch.setattr(mod, "AsyncSessionLocal", session_local)

    yield IsolatedDb(db_path, engine, async_engine, session_local, monkeypatch)

    # A module imported for the first time during the test bound the patched
    # factory from db.engine; monkeypatch knows nothing about it, so put the
    # real one back before the temp database goes away.
    for mod in list(sys.modules.values()):
        if getattr(mod, "AsyncSessionLocal", None) is session_local:
            try:
                mod.AsyncSessionLocal = real_session_local
            except (AttributeError, TypeError):
                pass
    try:
        asyncio.run(async_engine.dispose())
    except Exception:
        pass
    engine.dispose()
