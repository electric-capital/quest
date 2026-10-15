"""Tests for config.environment (run-mode resolution and capability flags)."""

import asyncio
import os
import shutil
import tempfile
import uuid
from importlib import reload
from pathlib import Path

import pytest

from config import environment


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Mode parsing
# ---------------------------------------------------------------------------

class TestGetQuestEnv:
    def test_default_is_prod(self, monkeypatch):
        monkeypatch.delenv("QUEST_ENV", raising=False)
        assert environment.get_quest_env() == "prod"

    @pytest.mark.parametrize("raw,expected", [
        ("local", "local"),
        ("staging", "staging"),
        ("prod", "prod"),
        # Legacy aliases
        ("dev", "local"),
        ("development", "local"),
        ("production", "prod"),
        # Normalization
        ("LOCAL", "local"),
        (" Staging ", "staging"),
        # Unknown values fail toward the most restrictive mode
        ("garbage", "prod"),
        ("", "prod"),
    ])
    def test_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("QUEST_ENV", raw)
        assert environment.get_quest_env() == expected

    def test_mode_predicates(self, monkeypatch):
        monkeypatch.setenv("QUEST_ENV", "local")
        assert environment.is_local() and not environment.is_staging() and not environment.is_prod()
        monkeypatch.setenv("QUEST_ENV", "staging")
        assert environment.is_staging() and not environment.is_local() and not environment.is_prod()
        monkeypatch.setenv("QUEST_ENV", "prod")
        assert environment.is_prod() and not environment.is_local() and not environment.is_staging()


# ---------------------------------------------------------------------------
# Capability flags
# ---------------------------------------------------------------------------

class TestCapabilityFlags:
    def test_dev_login_local_only(self, monkeypatch):
        monkeypatch.setenv("QUEST_ENV", "local")
        assert environment.allow_dev_login()
        for env in ("staging", "prod"):
            monkeypatch.setenv("QUEST_ENV", env)
            assert not environment.allow_dev_login()

    def test_domain_enforced_outside_local(self, monkeypatch):
        monkeypatch.setenv("QUEST_ENV", "local")
        assert not environment.enforce_domain()
        for env in ("staging", "prod"):
            monkeypatch.setenv("QUEST_ENV", env)
            assert environment.enforce_domain()

    def test_cookie_names_distinct_per_mode(self, monkeypatch):
        names = {}
        for env in ("local", "staging", "prod"):
            monkeypatch.setenv("QUEST_ENV", env)
            names[env] = environment.cookie_name()
        assert names["prod"] == "quest_session"
        assert names["local"] == "quest_session_local"
        assert names["staging"] == "quest_session_staging"
        assert len(set(names.values())) == 3

    @pytest.mark.parametrize("env,app_base_url,expected", [
        # Staging is always Secure, whatever the public URL.
        ("staging", "", True),
        ("staging", "http://quest.example.com", True),
        ("staging", "https://quest.example.com", True),
        # Prod and local follow the configured public URL's scheme.
        ("prod", "", False),
        ("prod", "http://quest.example.com", False),
        ("prod", "https://quest.example.com", True),
        ("prod", " HTTPS://Quest.Example.com", True),
        ("local", "", False),
        ("local", "http://10.0.0.5:9000", False),
        ("local", "https://dev.example.com", True),
        # Not a scheme match: an https host behind an http URL.
        ("prod", "http://https.example.com", False),
    ])
    def test_cookie_secure_matrix(self, monkeypatch, env, app_base_url, expected):
        import config.server_config as server_config
        monkeypatch.setenv("QUEST_ENV", env)
        monkeypatch.setattr(server_config, "get_app_base_url", lambda: app_base_url)
        assert environment.cookie_secure() is expected

    def test_cookie_secure_reads_app_base_url_env_override(self, monkeypatch, tmp_path):
        import config.server_config as server_config
        monkeypatch.setenv("QUEST_ENV", "prod")
        monkeypatch.setattr(server_config, "SERVER_CONFIG_FILE", tmp_path / "missing.json")
        monkeypatch.delenv("QUEST_APP_BASE_URL", raising=False)
        assert environment.cookie_secure() is False
        monkeypatch.setenv("QUEST_APP_BASE_URL", "https://quest.example.com/")
        assert environment.cookie_secure() is True

    def test_image_suffix_matches_mode(self, monkeypatch):
        monkeypatch.setenv("QUEST_ENV", "dev")  # legacy alias canonicalizes
        assert environment.image_suffix() == "local"


# ---------------------------------------------------------------------------
# check_user_allowed integration (domain bypass keyed off the environment)
# ---------------------------------------------------------------------------

class TestCheckUserAllowed:
    def test_local_allows_any_domain(self, monkeypatch):
        from chat.auth import check_user_allowed
        monkeypatch.setenv("QUEST_ENV", "local")
        assert check_user_allowed("anyone@example.com")

    @pytest.mark.parametrize("env", ["staging", "prod"])
    def test_non_local_enforces_domain(self, monkeypatch, env):
        from auth.config import ALLOWED_DOMAIN
        from chat.auth import check_user_allowed
        monkeypatch.setenv("QUEST_ENV", env)
        assert not check_user_allowed(f"anyone@not-{ALLOWED_DOMAIN}")
        assert check_user_allowed(f"someone@{ALLOWED_DOMAIN}")


# ---------------------------------------------------------------------------
# QUEST_DATA_DIR override in config.paths
# ---------------------------------------------------------------------------

class TestDataDirOverride:
    def test_env_var_wins(self, monkeypatch):
        import config.paths as paths_mod
        with tempfile.TemporaryDirectory(prefix="quest_data_dir_test_") as tmpdir:
            monkeypatch.setenv("QUEST_DATA_DIR", tmpdir)
            reload(paths_mod)
            assert paths_mod.DATA_DIR == Path(tmpdir)
            assert paths_mod.DATABASE_PATH == Path(tmpdir) / "quest.db"
        monkeypatch.delenv("QUEST_DATA_DIR")
        reload(paths_mod)
        assert paths_mod.DATA_DIR != Path(tmpdir)

    def test_relative_env_value_resolves_against_project_root(self, monkeypatch):
        import config.paths as paths_mod
        monkeypatch.setenv("QUEST_DATA_DIR", "data/local-runs/test-rel")
        reload(paths_mod)
        assert paths_mod.DATA_DIR == (paths_mod.PROJECT_ROOT / "data/local-runs/test-rel").resolve()
        monkeypatch.delenv("QUEST_DATA_DIR")
        reload(paths_mod)


# ---------------------------------------------------------------------------
# Local-mode admin default (canned admin without a server_config.json)
# ---------------------------------------------------------------------------

class TestLocalAdminDefault:
    def test_local_mode_defaults_canned_admin(self, monkeypatch, tmp_path):
        import config.server_config as gd
        monkeypatch.setattr(gd, "SERVER_CONFIG_FILE", tmp_path / "server_config.json")
        monkeypatch.setenv("QUEST_ENV", "local")
        config = gd.load_server_config()
        assert config["admin_emails"] == [environment.LOCAL_CANNED_ADMIN_EMAIL]

    def test_prod_mode_defaults_no_admins(self, monkeypatch, tmp_path):
        import config.server_config as gd
        monkeypatch.setattr(gd, "SERVER_CONFIG_FILE", tmp_path / "server_config.json")
        monkeypatch.setenv("QUEST_ENV", "prod")
        config = gd.load_server_config()
        assert config["admin_emails"] == []

    def test_explicit_admin_emails_override(self, monkeypatch, tmp_path):
        import config.server_config as gd
        cfg = tmp_path / "server_config.json"
        cfg.write_text('{"admin_emails": ["boss@example.com"]}')
        monkeypatch.setattr(gd, "SERVER_CONFIG_FILE", cfg)
        monkeypatch.setenv("QUEST_ENV", "local")
        config = gd.load_server_config()
        assert config["admin_emails"] == ["boss@example.com"]


# ---------------------------------------------------------------------------
# Dev-login endpoints gate on the run mode
# ---------------------------------------------------------------------------

@pytest.fixture()
def _isolated_db(monkeypatch):
    """Point engine + paths at a fresh sqlite file, then create the schema."""
    tmpdir = tempfile.mkdtemp(prefix="quest_env_test_")
    db_path = os.path.join(tmpdir, "quest.db")

    from config import paths
    monkeypatch.setattr(paths, "DATABASE_PATH", db_path, raising=True)

    import db.engine as engine_mod
    reload(engine_mod)
    import db.models as models_mod
    reload(models_mod)
    import db.user_store as user_store_mod
    reload(user_store_mod)

    models_mod.Base.metadata.create_all(engine_mod.engine)

    yield models_mod

    shutil.rmtree(tmpdir, ignore_errors=True)


class TestDevAccountsEndpoint:
    @pytest.mark.parametrize("env", ["staging", "prod"])
    def test_hidden_outside_local(self, monkeypatch, env):
        from fastapi import HTTPException
        from auth.dev_login import dev_accounts
        monkeypatch.setenv("QUEST_ENV", env)
        with pytest.raises(HTTPException) as exc:
            _run(dev_accounts())
        assert exc.value.status_code == 404

    def test_returns_roster_admins_first(self, monkeypatch, _isolated_db, tmp_path):
        import config.server_config as gd
        monkeypatch.setattr(gd, "SERVER_CONFIG_FILE", tmp_path / "server_config.json")
        monkeypatch.setenv("QUEST_ENV", "local")

        from db.engine import AsyncSessionLocal

        async def _seed():
            async with AsyncSessionLocal() as db:
                for email, name in [
                    ("zed@quest.local", "Zed"),
                    (environment.LOCAL_CANNED_ADMIN_EMAIL, "Ada Admin"),
                ]:
                    db.add(_isolated_db.User(
                        email=email, name=name, api_key=f"k-{uuid.uuid4().hex}",
                    ))
                await db.commit()

        _run(_seed())

        from auth.dev_login import dev_accounts
        result = _run(dev_accounts())
        accounts = result["accounts"]
        assert [a["email"] for a in accounts] == [
            environment.LOCAL_CANNED_ADMIN_EMAIL,
            "zed@quest.local",
        ]
        assert accounts[0]["is_admin"] is True
        assert accounts[1]["is_admin"] is False
