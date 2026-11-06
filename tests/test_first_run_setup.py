"""First-run setup (auth/first_run.py): the web flow that configures a
fresh staging/prod deployment.

Covers the "is setup needed" check, the encryption-password unlock with
its rate limit, answer validation, and what a completed setup writes
(server_config.json, the google_oauth store file, the Vertex key copy, the
first admin's account + password, the session cookie), plus the 404 once
the deployment is configured. The key-export half of run.py
(``setup_server_vertex_credentials``) is covered at the end.
"""

import asyncio
import json
import os
import stat

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import auth.config as auth_config
import auth.first_run as first_run
import auth.session as session_mod
import config.password_hashing as hashing
import config.paths as paths
import config.server_config as server_config
import config.service_credentials as sc
import db.password_store as password_store
import db.user_store as user_store
import run
from auth.config import COOKIE_NAME
from db.models import Base

PASSWORD = "quest-test-suite-password"  # the repo-root conftest's key password


def _run(coro):
    return asyncio.run(coro)


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture(autouse=True)
def _fast_scrypt(monkeypatch):
    monkeypatch.setattr(hashing, "_SCRYPT_N", 2 ** 10)


@pytest.fixture(autouse=True)
def deployment(tmp_path, monkeypatch):
    """A fresh prod-mode deployment: empty config, empty data dir, fresh DB."""
    monkeypatch.setenv("QUEST_ENV", "prod")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.delenv("GEMINI_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.delenv("QUEST_APP_BASE_URL", raising=False)
    config_file = tmp_path / "server_config.json"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(server_config, "SERVER_CONFIG_FILE", config_file)
    monkeypatch.setattr(paths, "DATA_DIR", data_dir)
    monkeypatch.setattr(sc, "SERVICE_CREDENTIALS_DIR", data_dir / "service_credentials")
    monkeypatch.setattr(sc, "LEGACY_CREDENTIALS_FILE", tmp_path / "server_credentials.json")
    monkeypatch.setattr(auth_config, "SERVER_CREDENTIALS_FILE", tmp_path / "server_credentials.json")
    monkeypatch.setattr(auth_config, "_server_credentials_cache", None)
    monkeypatch.setattr(session_mod, "get_secret_key", lambda: "test-secret")
    monkeypatch.setattr(first_run, "get_secret_key", lambda: "test-secret")
    for name in ("_unlock_by_ip", "_unlock_global"):
        old = getattr(first_run, name)
        monkeypatch.setattr(first_run, name, first_run.SlidingWindow(old.limit, old.window))

    db_path = data_dir / "quest.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{db_path}"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(user_store, "AsyncSessionLocal", factory)
    monkeypatch.setattr(password_store, "AsyncSessionLocal", factory)
    yield config_file, data_dir
    _run(engine.dispose())


def _write_config(config_file, **config):
    config_file.write_text(json.dumps(config))


def _client():
    app = FastAPI()
    app.include_router(first_run.router)
    return TestClient(app)


def _unlock(client, password=PASSWORD):
    return client.post("/app/api/setup/unlock", json={"password": password})


def _complete(client, token, **answers):
    return client.post("/app/api/setup/complete", json={"token": token, **answers})


# ---------------------------------------------------------------------------
# Is setup required?
# ---------------------------------------------------------------------------

class TestSetupRequired:
    def test_fresh_deployment_needs_setup(self):
        assert _run(first_run.missing_required_config()) == [
            first_run.MISSING_ADMIN_EMAILS, first_run.MISSING_SIGN_IN_METHOD,
        ]
        assert _run(first_run.setup_required())

    def test_never_in_local_mode(self, monkeypatch):
        monkeypatch.setenv("QUEST_ENV", "local")
        assert not _run(first_run.setup_required())

    def test_google_mode_needs_oauth_client(self, deployment):
        config_file, _ = deployment
        _write_config(config_file, admin_emails=["ops@otherco.com"], login_method="google")
        assert _run(first_run.missing_required_config()) == [first_run.MISSING_GOOGLE_OAUTH]
        sc.write_service_credentials("google_oauth", {"web": {"client_id": "x", "client_secret": "y"}})
        assert _run(first_run.missing_required_config()) == []
        assert not _run(first_run.setup_required())

    def test_legacy_google_oauth_counts(self, deployment, tmp_path):
        config_file, _ = deployment
        _write_config(config_file, admin_emails=["ops@otherco.com"])
        (tmp_path / "server_credentials.json").write_text(
            json.dumps({"google_oauth": {"web": {"client_id": "x", "client_secret": "y"}}})
        )
        assert _run(first_run.missing_required_config()) == []

    def test_password_mode_needs_an_admin_with_a_password(self, deployment):
        config_file, _ = deployment
        _write_config(config_file, admin_emails=["ops@otherco.com"], login_method="password")
        assert _run(first_run.missing_required_config()) == [first_run.MISSING_ADMIN_PASSWORD]
        _run(user_store.create_user(email="ops@otherco.com", name="Ops", api_key="k"))
        _run(password_store.set_password_hash("ops@otherco.com", hashing.hash_password("hunter2hunter2")))
        assert _run(first_run.missing_required_config()) == []

    def test_check_failure_fails_closed(self, monkeypatch):
        async def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(first_run, "missing_required_config", _boom)
        assert not _run(first_run.setup_required())


# ---------------------------------------------------------------------------
# Unlock
# ---------------------------------------------------------------------------

class TestUnlock:
    def test_wrong_password_is_refused(self):
        resp = _unlock(_client(), "not it")
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "wrong_password"

    def test_right_password_returns_token_and_defaults(self):
        resp = _unlock(_client())
        assert resp.status_code == 200
        body = resp.json()
        assert first_run.setup_token_valid(body["token"])
        defaults = body["defaults"]
        assert defaults["login_method"] == "password"
        assert not defaults["login_method_configured"]
        assert defaults["admin_emails"] == []
        assert defaults["app_base_url"] == "http://testserver"
        assert not defaults["google_oauth_configured"]
        assert defaults["redirect_paths"] == list(first_run.OAUTH_REDIRECT_PATHS)
        inference = defaults["inference"]
        assert not inference["llm_configured"]
        assert {k["kind"] for k in inference["kinds"]} >= {"anthropic", "openrouter", "local"}
        assert inference["api_types"][0]["id"] == "openai"
        assert inference["vertex"]["credentials_source"] is None
        assert inference["vertex"]["configured_project_id"] == ""
        assert defaults["public_email_domains"] == sorted(first_run.PUBLIC_EMAIL_DOMAINS)

    def test_defaults_reflect_partial_config(self, deployment):
        config_file, _ = deployment
        _write_config(
            config_file, allowed_login_domain="otherco.com", app_base_url="https://q.otherco.com/",
            allowed_login_emails=["kid@gmail.com"], anthropic={"vertex_project_id": "proj"},
        )
        sc.write_service_credentials("google_oauth", {"web": {"client_id": "cid", "client_secret": "s"}})
        defaults = _unlock(_client()).json()["defaults"]
        # Google OAuth already configured, no explicit method: a pre-password
        # install, so Google sign-in is the suggested method.
        assert defaults["login_method"] == "google"
        assert defaults["google_oauth_configured"]
        assert defaults["google_client_id"] == "cid"
        assert defaults["allowed_login_domain"] == "otherco.com"
        assert defaults["allowed_login_emails"] == ["kid@gmail.com"]
        assert defaults["app_base_url"] == "https://q.otherco.com"
        assert defaults["app_base_url_configured"]
        assert defaults["inference"]["llm_configured"]
        assert defaults["inference"]["vertex"]["configured_project_id"] == "proj"

    def test_empty_password_is_refused_without_kdf(self):
        assert _unlock(_client(), "   ").status_code == 401

    def test_repeated_failures_are_rate_limited(self):
        client = _client()
        for _ in range(first_run._unlock_by_ip.limit):
            assert _unlock(client, "nope").status_code == 401
        resp = _unlock(client)
        assert resp.status_code == 429
        assert resp.json()["detail"]["error"] == "rate_limited"

    def test_token_is_rejected_when_forged_or_expired(self, monkeypatch):
        assert not first_run.setup_token_valid("garbage")
        token = first_run.mint_setup_token()
        assert first_run.setup_token_valid(token)
        monkeypatch.setattr(first_run, "TOKEN_MAX_AGE_SECONDS", -1)
        assert not first_run.setup_token_valid(token)


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------

class TestComplete:
    def test_requires_token(self):
        resp = _complete(_client(), "garbage", admin_emails=["ops@otherco.com"], admin_password="x" * 12)
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "setup_locked"

    def test_password_sign_in_writes_everything_and_signs_in(self, deployment):
        config_file, data_dir = deployment
        _write_config(config_file, custom_section={"api_base": "https://internal"},
                      anthropic={"vertex_region": "europe-west1"})
        client = _client()
        token = _unlock(client).json()["token"]
        resp = _complete(
            client, token,
            login_method="password",
            admin_emails=["Me@Gmail.com", "ops@otherco.com"],
            allowed_login_domain="@OtherCo.com",
            allowed_login_emails=["friend@gmail.com"],
            app_base_url="quest.otherco.com/",
            admin_password="correct horse battery",
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"login_method": "password", "signed_in": True}
        # The first admin is signed in with a password-bound cookie.
        payload = session_mod.get_session_serializer().loads(client.cookies[COOKIE_NAME])
        assert payload["pw"]
        user = _run(user_store.get_user_by_email("me@gmail.com"))
        assert user and user["id"] == payload["uid"]
        stored = _run(password_store.get_password_hash("me@gmail.com"))
        assert hashing.verify_password("correct horse battery", stored)

        config = json.loads(config_file.read_text())
        assert config["login_method"] == "password"
        assert config["admin_emails"] == ["me@gmail.com", "ops@otherco.com"]
        assert config["allowed_login_domain"] == "otherco.com"
        # The gmail admin is outside the domain, so it is allow-listed.
        assert config["allowed_login_emails"] == ["friend@gmail.com", "me@gmail.com"]
        assert config["app_base_url"] == "https://quest.otherco.com"
        assert config["anthropic"] == {"vertex_region": "europe-west1"}
        assert config["custom_section"] == {"api_base": "https://internal"}
        assert not (data_dir / "service_credentials" / "google_oauth.json").exists()

        assert _run(first_run.missing_required_config()) == []
        # The flow is closed from now on.
        assert _unlock(client).status_code == 404
        assert _complete(client, token, admin_emails=["x@otherco.com"]).status_code == 404

    def test_google_sign_in_writes_the_oauth_client(self, deployment):
        config_file, data_dir = deployment
        client = _client()
        token = _unlock(client).json()["token"]
        resp = _complete(
            client, token,
            login_method="google",
            admin_emails=["ops@otherco.com"],
            allowed_login_domain="otherco.com",
            app_base_url="https://quest.otherco.com",
            google_client_id="cid.apps.googleusercontent.com",
            google_client_secret="sekrit",
            google_project_id="otherco-oauth",
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"login_method": "google", "signed_in": False}
        assert COOKIE_NAME not in client.cookies
        stored = sc.read_service_credentials("google_oauth")
        web = stored["web"]
        assert web["client_id"] == "cid.apps.googleusercontent.com"
        assert web["client_secret"] == "sekrit"
        assert web["project_id"] == "otherco-oauth"
        assert web["auth_uri"] == first_run.GOOGLE_OAUTH_WEB_DEFAULTS["auth_uri"]
        assert web["redirect_uris"] == [
            "https://quest.otherco.com/auth/callback",
            "https://quest.otherco.com/auth/google-services/callback",
        ]
        # Encrypted on disk.
        raw = json.loads((data_dir / "service_credentials" / "google_oauth.json").read_text())
        assert set(raw) == {"encrypted"}
        config = json.loads(config_file.read_text())
        assert config["login_method"] == "google"
        assert config["allowed_login_emails"] == []
        assert "anthropic" not in config
        assert _run(first_run.missing_required_config()) == []

    def test_google_sign_in_keeps_a_stored_client(self, deployment):
        """Admin emails missing but the OAuth client already stored: the
        client id may be left out, or repeated with an empty secret."""
        sc.write_service_credentials("google_oauth", {"web": {"client_id": "cid", "client_secret": "old"}})
        client = _client()
        token = _unlock(client).json()["token"]
        resp = _complete(
            client, token, login_method="google", admin_emails=["ops@otherco.com"],
            allowed_login_domain="otherco.com", google_client_id="cid",
        )
        assert resp.status_code == 200, resp.text
        assert sc.read_service_credentials("google_oauth")["web"]["client_secret"] == "old"

    def test_only_the_first_admin_gets_the_password(self, deployment):
        client = _client()
        token = _unlock(client).json()["token"]
        resp = _complete(
            client, token, admin_emails=["a@otherco.com", "b@otherco.com"],
            allowed_login_domain="otherco.com", admin_password="correct horse",
        )
        assert resp.status_code == 200, resp.text
        assert _run(password_store.get_password_hash("a@otherco.com"))
        assert _run(user_store.get_user_by_email("b@otherco.com")) is None

    @pytest.mark.parametrize("answers, field", [
        ({"login_method": "ldap"}, "login_method"),
        ({"admin_emails": []}, "admin_emails"),
        ({"admin_emails": ["not-an-email"]}, "admin_emails"),
        ({"admin_emails": ["a" * 250 + "@otherco.com"]}, "admin_emails"),
        ({"allowed_login_domain": "a" * 250 + ".otherco.com"}, "allowed_login_domain"),
        ({"allowed_login_domain": "gmail.com"}, "allowed_login_domain"),
        ({"allowed_login_domain": "not a domain"}, "allowed_login_domain"),
        ({"allowed_login_emails": ["nope"]}, "allowed_login_emails"),
        ({"app_base_url": "ftp://x"}, "app_base_url"),
        ({"admin_password": "short"}, "admin_password"),
        ({"admin_password": ""}, "admin_password"),
        ({"login_method": "google", "google_client_id": ""}, "google_client_id"),
        ({"login_method": "google", "google_client_id": "cid", "google_client_secret": ""}, "google_client_secret"),
    ])
    def test_invalid_answers_name_the_field(self, deployment, answers, field):
        config_file, data_dir = deployment
        client = _client()
        token = _unlock(client).json()["token"]
        base = {
            "login_method": "password", "admin_emails": ["ops@otherco.com"],
            "allowed_login_domain": "otherco.com", "admin_password": "correct horse",
        }
        resp = _complete(client, token, **{**base, **answers})
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "invalid_params"
        assert detail["field"] == field
        # Nothing was written.
        assert not config_file.exists()
        assert not (data_dir / "service_credentials").exists()
        assert _run(user_store.get_user_by_email("ops@otherco.com")) is None

    def test_closed_once_configured(self, deployment):
        config_file, _ = deployment
        _write_config(config_file, admin_emails=["ops@otherco.com"], login_method="google")
        sc.write_service_credentials("google_oauth", {"web": {"client_id": "x", "client_secret": "y"}})
        resp = _unlock(_client())
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "setup_not_required"


class TestModelsStep:
    """The optional models step: an API-key provider through the shared
    quick setup, or Vertex AI from a key / the environment's ADC."""

    @pytest.fixture
    def quick_setup(self, monkeypatch):
        import chat.llm.model_setup as model_setup
        calls = []

        async def fake(kind, *, api_key="", base_url="", api_type=""):
            calls.append({"kind": kind, "api_key": api_key, "base_url": base_url, "api_type": api_type})
            if kind == "nope":
                raise model_setup.QuickSetupError("unknown_kind", "Unknown inference provider kind: nope")
            if not api_key:
                raise model_setup.QuickSetupError("invalid_params", "An API key is required for Anthropic.")
            return {
                "instance": {"id": "anthropic", "kind": "anthropic", "label": "", "models": []},
                "added": ["anthropic:claude-sonnet-5"],
                "check": {"model": "anthropic:claude-sonnet-5", "ok": api_key != "bad", "error": None if api_key != "bad" else "401"},
                "catalog_error": None,
            }

        monkeypatch.setattr(model_setup, "quick_setup_provider", fake)
        return calls

    @pytest.fixture
    def health(self, monkeypatch):
        import chat.llm.health as health_mod
        checked, scheduled = [], []

        class Store:
            async def run_check(self, model_id):
                checked.append(model_id)
                return {"model": model_id, "ok": True, "error": None, "checked_at": "now"}

        monkeypatch.setattr(health_mod, "get_model_health_store", lambda: Store())
        monkeypatch.setattr(health_mod, "schedule_model_rechecks", lambda ids: scheduled.append(list(ids)))
        return checked, scheduled

    def _token(self, client):
        return _unlock(client).json()["token"]

    def test_provider_requires_token(self):
        resp = _client().post("/app/api/setup/inference/provider", json={"token": "x", "kind": "anthropic", "api_key": "k"})
        assert resp.status_code == 401

    def test_provider_connects_through_quick_setup(self, quick_setup):
        client = _client()
        resp = client.post("/app/api/setup/inference/provider", json={
            "token": self._token(client), "kind": "anthropic", "api_key": "sk-ant-good",
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["instance"] == {"id": "anthropic", "kind": "anthropic", "label": "Anthropic"}
        assert body["added"] == ["anthropic:claude-sonnet-5"]
        assert body["check"]["ok"]
        assert quick_setup == [{"kind": "anthropic", "api_key": "sk-ant-good", "base_url": "", "api_type": ""}]

    def test_provider_reports_quick_setup_errors(self, quick_setup):
        client = _client()
        token = self._token(client)
        resp = client.post("/app/api/setup/inference/provider", json={"token": token, "kind": "nope", "api_key": "k"})
        assert resp.status_code == 400 and resp.json()["detail"]["error"] == "unknown_kind"
        resp = client.post("/app/api/setup/inference/provider", json={"token": token, "kind": "anthropic"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == {
            "error": "invalid_params", "message": "An API key is required for Anthropic.", "field": "api_key",
        }

    def test_vertex_key_is_stored_exported_and_checked(self, deployment, health):
        config_file, data_dir = deployment
        _write_config(config_file, anthropic={"vertex_region": "europe-west1"})
        checked, scheduled = health
        client = _client()
        resp = client.post("/app/api/setup/inference/vertex", json={
            "token": self._token(client),
            "service_account": {"type": "service_account", "project_id": "otherco-quest"},
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["project_id"] == "otherco-quest"
        assert body["credentials_source"] == "env"
        assert body["check"]["ok"] and body["check"]["model"] == checked[0]
        assert scheduled and checked[0] not in scheduled[0]
        key_copy = data_dir / first_run.VERTEX_KEY_FILENAME
        assert _mode(key_copy) == 0o600
        assert json.loads(key_copy.read_text())["project_id"] == "otherco-quest"
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(key_copy)
        config = json.loads(config_file.read_text())
        assert config["anthropic"] == {"vertex_region": "europe-west1", "vertex_project_id": "otherco-quest"}
        assert _unlock(client).json()["defaults"]["inference"]["vertex"] == {
            "credentials_source": "env", "service_account_email": None,
            "detected_project_id": "otherco-quest", "problem": None,
            "configured_project_id": "otherco-quest",
        }

    def test_vertex_from_environment_credentials_needs_only_a_project(self, deployment, health, tmp_path, monkeypatch):
        config_file, data_dir = deployment
        adc = tmp_path / "adc.json"
        adc.write_text(json.dumps({"type": "service_account", "project_id": "env-proj", "client_email": "sa@env"}))
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc))
        client = _client()
        token = self._token(client)
        defaults = _unlock(client).json()["defaults"]["inference"]["vertex"]
        assert defaults["credentials_source"] == "env"
        assert defaults["detected_project_id"] == "env-proj"
        assert defaults["service_account_email"] == "sa@env"
        resp = client.post("/app/api/setup/inference/vertex", json={"token": token, "project_id": "env-proj"})
        assert resp.status_code == 200, resp.text
        assert json.loads(config_file.read_text())["anthropic"]["vertex_project_id"] == "env-proj"
        assert not (data_dir / first_run.VERTEX_KEY_FILENAME).exists()
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(adc)

    @pytest.mark.parametrize("body, field", [
        ({}, "service_account"),                                   # no key, no ADC
        ({"project_id": "p"}, "service_account"),
        ({"service_account": {"type": "authorized_user"}}, "service_account"),
        ({"service_account": {"type": "service_account"}}, "project_id"),
    ])
    def test_vertex_invalid_answers_name_the_field(self, deployment, health, body, field):
        config_file, data_dir = deployment
        client = _client()
        resp = client.post("/app/api/setup/inference/vertex", json={"token": self._token(client), **body})
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["field"] == field
        assert not config_file.exists()
        assert not (data_dir / first_run.VERTEX_KEY_FILENAME).exists()


class TestNormalizeBaseUrl:
    @pytest.mark.parametrize("raw, expected", [
        ("", ""),
        ("quest.example.com", "https://quest.example.com"),
        ("https://quest.example.com/", "https://quest.example.com"),
        ("http://10.0.0.5:8000", "http://10.0.0.5:8000"),
    ])
    def test_normalizes(self, raw, expected):
        assert first_run.normalize_base_url(raw) == expected

    @pytest.mark.parametrize("raw", ["ftp://x", "https://", "https://x?y=1", "https://x#frag"])
    def test_rejects(self, raw):
        with pytest.raises(ValueError):
            first_run.normalize_base_url(raw)


# ---------------------------------------------------------------------------
# run.py: exporting the setup-written key on later starts
# ---------------------------------------------------------------------------

class TestServerVertexCredentials:
    def test_exports_key_when_present(self, deployment, monkeypatch):
        _, data_dir = deployment
        key_path = data_dir / run.LEGACY_VERTEX_CREDENTIALS_FILENAME
        key_path.write_text(json.dumps({"type": "service_account"}))
        run.setup_server_vertex_credentials(data_dir)
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(key_path)

    def test_external_env_wins(self, deployment, monkeypatch):
        _, data_dir = deployment
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/elsewhere/key.json")
        key_path = data_dir / run.LEGACY_VERTEX_CREDENTIALS_FILENAME
        key_path.write_text(json.dumps({"type": "service_account"}))
        run.setup_server_vertex_credentials(data_dir)
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/elsewhere/key.json"

    def test_no_key_no_export(self, deployment):
        _, data_dir = deployment
        run.setup_server_vertex_credentials(data_dir)
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ
