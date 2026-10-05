"""Quest Docs prompting and the account-delete sweep.

Covers:

1. ``get_user_connected_services()`` carries the ``docs`` pseudo-key, which
   follows the per-user Quest Docs feature gate (incl. the allowed-users
   list); GET /me's ``has_any_service_connected`` ignores pseudo-keys.
2. The ``system:quest_docs`` system skill: gated on the pseudo-key, content
   covers the seven tools and the ``write_doc`` handoff, and the generic
   action-request docs point at it.
3. Prompt gating: the top-level Dynamic Tools section lists the seven doc
   tools only while the gate is open; the read-only tiers (sub-agents,
   cross-user subagents, inference API runs) never document the four write
   tools but keep the reads.
4. POST /delete-account removes every doc directory the user owns (user and
   project docs), collected before the rows cascade away, best-effort per id.

The public-project prompt's ``docs_enabled`` behavior is covered beside the
rest of the public prompt in tests/test_public_projects.py.
"""

import asyncio
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config.feature_gates as fg
from chat.docs.constants import DOCS_SERVICE_KEY

DOC_TOOLS = (
    "list_docs", "search_docs", "read_doc", "create_doc", "edit_doc",
    "append_to_doc", "add_doc_image",
)
READ_TOOLS = ("list_docs", "search_docs", "read_doc")
WRITE_TOOLS = ("create_doc", "edit_doc", "append_to_doc", "add_doc_image")

BASE_URL = "http://localhost:8000"
API_KEY = "test-api-key"


def _run(coro):
    return asyncio.run(coro)


def _bullet(name: str) -> str:
    """How _build_dynamic_tools_section documents a callable tool."""
    return f"- **{name}** --"


@pytest.fixture()
def gates_file(tmp_path, monkeypatch):
    """Per-test feature gates file (missing initially = every gate closed)."""
    path = tmp_path / "feature_gates.json"
    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", path)
    return path


@pytest.fixture()
def no_plugins():
    """No loaded plugins, so the connected-services map is core + docs only."""
    from config import plugins as plugins_mod
    with patch.object(plugins_mod, "_LOADED", []), \
            patch("config.service_credentials.read_service_credentials",
                  return_value=None):
        yield


def _connected(user):
    from api.instructions import get_user_connected_services
    return get_user_connected_services(user)


# ---------------------------------------------------------------------------
# 1. Connected-services pseudo-key + GET /me aggregate
# ---------------------------------------------------------------------------


class TestConnectedServicesDocsKey:
    def test_pseudo_key_constant(self):
        from api.instructions import PSEUDO_SERVICE_KEYS
        assert PSEUDO_SERVICE_KEYS == frozenset({DOCS_SERVICE_KEY})
        assert DOCS_SERVICE_KEY == "docs"

    def test_follows_gate(self, gates_file, no_plugins):
        user = {"email": "u@example.com"}
        assert _connected(user)[DOCS_SERVICE_KEY] is False
        fg.set_feature_enabled(fg.FEATURE_DOCS, True)
        assert _connected(user)[DOCS_SERVICE_KEY] is True
        fg.set_feature_enabled(fg.FEATURE_DOCS, False)
        assert _connected(user)[DOCS_SERVICE_KEY] is False

    def test_follows_allowed_users(self, gates_file, no_plugins):
        fg.set_feature_enabled(fg.FEATURE_DOCS, True)
        fg.set_feature_allowed_users(fg.FEATURE_DOCS, ["ada@example.com"])
        assert _connected({"email": "Ada@Example.com"})[DOCS_SERVICE_KEY] is True
        assert _connected({"email": "bob@example.com"})[DOCS_SERVICE_KEY] is False
        # No email at all never matches an allowed-users list.
        assert _connected({})[DOCS_SERVICE_KEY] is False

    def test_other_services_unaffected(self, gates_file, no_plugins):
        fg.set_feature_enabled(fg.FEATURE_DOCS, True)
        services = _connected({"email": "u@example.com", "airtable_token": "pat"})
        assert services["airtable"] is True
        assert services["google_services"] is False
        assert services[DOCS_SERVICE_KEY] is True


class TestMeAggregate:
    def _me(self, user):
        import chat.routes.user as user_routes
        return _run(user_routes.get_current_user_info(user=user))

    def test_docs_alone_is_not_a_connected_service(self, gates_file, no_plugins):
        fg.set_feature_enabled(fg.FEATURE_DOCS, True)
        user = {"email": "u@example.com", "name": "U"}
        assert _connected(user)[DOCS_SERVICE_KEY] is True
        info = self._me(user)
        assert info["has_any_service_connected"] is False
        assert info["google_services_connected"] is False

    def test_real_service_still_counts(self, gates_file, no_plugins):
        fg.set_feature_enabled(fg.FEATURE_DOCS, True)
        info = self._me({"email": "u@example.com", "name": "U", "airtable_token": "pat"})
        assert info["has_any_service_connected"] is True


# ---------------------------------------------------------------------------
# 2. system:quest_docs
# ---------------------------------------------------------------------------


class TestQuestDocsSkill:
    def test_registration(self):
        from chat.system_skills import CATALOG
        skill = CATALOG["system:quest_docs"]
        assert skill.name == "Quest Docs"
        assert skill.requires == DOCS_SERVICE_KEY
        assert skill.requires_project is False
        assert 0 < len(skill.description) <= 120
        # system:docs stays the Google Docs skill.
        assert CATALOG["system:docs"].name == "Google Docs"

    def test_gated_on_docs_pseudo_key(self):
        from chat.system_skills import list_system_skills
        on = {s.id for s in list_system_skills({DOCS_SERVICE_KEY: True})}
        off = {s.id for s in list_system_skills({DOCS_SERVICE_KEY: False})}
        missing = {s.id for s in list_system_skills({})}
        assert "system:quest_docs" in on
        assert "system:quest_docs" not in off
        assert "system:quest_docs" not in missing

    def test_enumeration_and_load_follow_gate(self):
        from chat.system_skills import (
            build_system_skills_enumeration,
            load_system_skills,
        )
        assert "system:quest_docs" in build_system_skills_enumeration({DOCS_SERVICE_KEY: True})
        assert "system:quest_docs" not in build_system_skills_enumeration({DOCS_SERVICE_KEY: False})

        [loaded] = load_system_skills(
            ["system:quest_docs"], {DOCS_SERVICE_KEY: True}, BASE_URL, API_KEY,
        )
        assert "content" in loaded and "error" not in loaded
        [refused] = load_system_skills(
            ["system:quest_docs"], {DOCS_SERVICE_KEY: False}, BASE_URL, API_KEY,
        )
        assert "error" in refused and "content" not in refused

    def test_content_covers_tools_and_handoff(self):
        from chat.docs.access import (
            APPROVAL_WRITE_NOTE,
            DENY_PUBLIC_DOC_FROM_PRIVATE,
            DENY_READ_ONLY_SHARE,
            DENY_SLACK_NEEDS_APPROVAL,
        )
        from chat.system_skills import CATALOG
        content = CATALOG["system:quest_docs"].content_builder(BASE_URL, API_KEY)
        for name in DOC_TOOLS:
            assert name in content, name
        assert "write_doc" in content
        assert 'request_type="write_doc"' in content
        assert "approval_required" in content
        assert "suggested_request" in content
        # The three forwarded param shapes.
        for op in ('"operation": "edit"', '"operation": "append"', '"operation": "add_image"'):
            assert op in content, op
        # Quoted access-rule texts stay in sync with chat/docs/access.py.
        for text in (
            APPROVAL_WRITE_NOTE, DENY_PUBLIC_DOC_FROM_PRIVATE,
            DENY_READ_ONLY_SHARE, DENY_SLACK_NEEDS_APPROVAL,
        ):
            assert text in content, text
        # Images, paging and the routine pattern.
        assert "assets/<name>" in content
        assert 'placement: "none"' in content
        assert "start_line" in content and "end_line" in content
        assert "200,000" in content
        assert "## YYYY-MM-DD" in content
        assert "Slack" in content

    def test_action_request_docs_point_at_skill(self):
        from chat.llm.tool_schemas import TOP_LEVEL_TOOLS
        from chat.system_skills import CATALOG
        index = CATALOG["system:action_requests"].content_builder(BASE_URL, API_KEY)
        assert "`write_doc`" in index and "system:quest_docs" in index
        [car] = [t for t in TOP_LEVEL_TOOLS if t["name"] == "create_action_request"]
        assert "system:quest_docs" in car["description"]


# ---------------------------------------------------------------------------
# 3. Prompt gating per tier
# ---------------------------------------------------------------------------


class TestPromptGating:
    def test_helper_rosters(self):
        from chat.gemini_api.system_prompt import _doc_tool_names, _doc_write_tool_names
        assert _doc_tool_names() == frozenset(DOC_TOOLS)
        assert _doc_write_tool_names() == frozenset(WRITE_TOOLS)

    @pytest.mark.parametrize("docs", [False, True])
    def test_top_level_dynamic_tools_follow_pseudo_key(self, docs):
        from chat.gemini_api.system_prompt import get_system_prompt
        prompt = get_system_prompt(
            API_KEY, base_url=BASE_URL,
            connected_services={DOCS_SERVICE_KEY: docs},
        )
        for name in DOC_TOOLS:
            assert (_bullet(name) in prompt) is docs, name
        assert ("system:quest_docs" in prompt) is docs

    def test_top_level_end_to_end_with_gate(self, gates_file, no_plugins):
        from chat.gemini_api.system_prompt import get_system_prompt
        user = {"email": "u@example.com"}
        closed = get_system_prompt(API_KEY, connected_services=_connected(user))
        assert not any(_bullet(n) in closed for n in DOC_TOOLS)
        fg.set_feature_enabled(fg.FEATURE_DOCS, True)
        opened = get_system_prompt(API_KEY, connected_services=_connected(user))
        assert all(_bullet(n) in opened for n in DOC_TOOLS)
        # A capability, not a connector: the proxy preamble is unchanged.
        assert "No API services are currently connected" in opened

    @pytest.mark.parametrize("can_nest", [False, True])
    @pytest.mark.parametrize("has_project", [False, True])
    def test_sub_agent_prompt_reads_only(self, can_nest, has_project):
        from chat.gemini_api.system_prompt import get_sub_agent_system_prompt
        prompt = get_sub_agent_system_prompt(
            "helper", API_KEY, base_url=BASE_URL,
            connected_services={DOCS_SERVICE_KEY: True},
            has_project=has_project, can_nest=can_nest,
        )
        for name in WRITE_TOOLS:
            assert _bullet(name) not in prompt, name
        for name in READ_TOOLS:
            assert _bullet(name) in prompt, name

    def test_user_subagent_prompt_reads_only(self):
        from chat.gemini_api.system_prompt import get_user_subagent_system_prompt
        prompt = get_user_subagent_system_prompt(
            API_KEY, base_url=BASE_URL,
            connected_services={DOCS_SERVICE_KEY: True},
            target_user_email="t@example.com", caller_email="c@example.com",
        )
        for name in WRITE_TOOLS:
            assert _bullet(name) not in prompt, name
        for name in READ_TOOLS:
            assert _bullet(name) in prompt, name

    def test_inference_prompt_reads_only(self):
        from chat.gemini_api.system_prompt import get_inference_api_system_prompt
        prompt = get_inference_api_system_prompt(
            "", base_url=BASE_URL, user_email="u@example.com",
            connected_services={DOCS_SERVICE_KEY: True},
        )
        for name in WRITE_TOOLS:
            assert _bullet(name) not in prompt, name
        for name in READ_TOOLS:
            assert _bullet(name) in prompt, name

    def test_read_only_tiers_hide_reads_when_gate_closed(self):
        from chat.gemini_api.system_prompt import (
            get_inference_api_system_prompt,
            get_sub_agent_system_prompt,
            get_user_subagent_system_prompt,
        )
        closed = {DOCS_SERVICE_KEY: False}
        prompts = [
            get_sub_agent_system_prompt("helper", API_KEY, connected_services=closed),
            get_user_subagent_system_prompt(API_KEY, connected_services=closed),
            get_inference_api_system_prompt("", connected_services=closed),
        ]
        for prompt in prompts:
            for name in DOC_TOOLS:
                assert _bullet(name) not in prompt, name
            assert "system:quest_docs" not in prompt


# ---------------------------------------------------------------------------
# 4. Account delete sweeps the doc directories
# ---------------------------------------------------------------------------


def _fk_on(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture()
def account_env(tmp_path, monkeypatch):
    """Isolated DB (foreign keys on, like db/engine.py) for every store the
    delete-account route touches, plus isolated chat/project/doc dirs."""
    db_path = tmp_path / "quest.db"
    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(sync_engine, "connect", _fk_on)
    event.listen(async_engine.sync_engine, "connect", _fk_on)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import chat.storage as storage_mod
    import db.action_request_store as action_request_store
    import db.conversation_store as conversation_store
    import db.doc_store as doc_store
    import db.guide_store as guide_store
    import db.memory_store as memory_store
    import db.models as models
    import db.project_store as project_store
    import db.routine_store as routine_store
    import db.user_store as user_store

    models.Base.metadata.create_all(sync_engine)
    for mod in (
        action_request_store, conversation_store, doc_store, guide_store,
        memory_store, project_store, routine_store, user_store,
    ):
        monkeypatch.setattr(mod, "AsyncSessionLocal", session_local)

    dirs = {name: tmp_path / name for name in ("chats", "projects", "docs")}
    for path in dirs.values():
        path.mkdir()
    monkeypatch.setattr(storage_mod, "CHATS_DIR", dirs["chats"])
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", dirs["projects"])
    monkeypatch.setattr(storage_mod, "DOCS_DIR", dirs["docs"])

    async def _seed():
        async with session_local() as db:
            users = [
                models.User(email=f"{name}@example.com", api_key=f"k-{uuid.uuid4().hex}")
                for name in ("alice", "bob")
            ]
            db.add_all(users)
            await db.commit()
            for u in users:
                await db.refresh(u)
            return {u.email.split("@")[0]: {"id": u.id, "email": u.email} for u in users}

    users = _run(_seed())
    yield {"users": users, "dirs": dirs, "doc_store": doc_store,
           "project_store": project_store}
    _run(async_engine.dispose())
    sync_engine.dispose()


def _seed_doc(env, owner, title, *, project_id=None, with_dir=True):
    from chat.docs import files
    doc_id = str(uuid.uuid4())
    size = files.init_doc(doc_id, f"# {title}\n") if with_dir else 0
    _run(env["doc_store"].create_doc(
        env["users"][owner]["id"], title, mode="private",
        project_id=project_id, content_size=size, doc_id=doc_id,
    ))
    return doc_id


class TestDeleteAccountSweep:
    def _delete(self, env, who="alice"):
        import chat.routes.user as user_routes
        return _run(user_routes.delete_account(request=None, user=env["users"][who]))

    def _seed_alice_and_bob(self, env):
        project = _run(env["project_store"].create_project(
            env["users"]["alice"]["id"], "Research",
        ))
        return {
            "alice_user_doc": _seed_doc(env, "alice", "Notes"),
            "alice_project_doc": _seed_doc(env, "alice", "Log", project_id=project["id"]),
            "alice_no_dir": _seed_doc(env, "alice", "Ghost", with_dir=False),
            "bob_doc": _seed_doc(env, "bob", "Bob notes"),
        }

    def test_removes_user_and_project_doc_dirs(self, account_env):
        docs_dir = account_env["dirs"]["docs"]
        ids = self._seed_alice_and_bob(account_env)
        assert (docs_dir / ids["alice_project_doc"] / "doc.md").is_file()

        response = self._delete(account_env)
        assert response.status_code == 200

        assert not (docs_dir / ids["alice_user_doc"]).exists()
        # Project docs cascade with the project row before the user row is
        # deleted; their ids were collected first, so the dir still goes.
        assert not (docs_dir / ids["alice_project_doc"]).exists()
        assert not (docs_dir / ids["alice_no_dir"]).exists()
        # Other users' docs are untouched.
        assert (docs_dir / ids["bob_doc"] / "doc.md").is_file()

        store = account_env["doc_store"]
        for key in ("alice_user_doc", "alice_project_doc", "alice_no_dir"):
            assert _run(store.get_doc(ids[key])) is None, key
        assert _run(store.get_doc(ids["bob_doc"])) is not None

    def test_sweep_is_best_effort_per_doc(self, account_env, monkeypatch):
        from chat.docs import files
        docs_dir = account_env["dirs"]["docs"]
        ids = self._seed_alice_and_bob(account_env)

        real_delete = files.delete_doc_dir
        attempted: list[str] = []

        def flaky_delete(doc_id):
            attempted.append(doc_id)
            if doc_id == ids["alice_user_doc"]:
                raise OSError("disk on fire")
            real_delete(doc_id)

        monkeypatch.setattr(files, "delete_doc_dir", flaky_delete)
        response = self._delete(account_env)
        assert response.status_code == 200

        assert set(attempted) == {
            ids["alice_user_doc"], ids["alice_project_doc"], ids["alice_no_dir"],
        }
        # The failing id is left behind; the others are still swept.
        assert (docs_dir / ids["alice_user_doc"]).exists()
        assert not (docs_dir / ids["alice_project_doc"]).exists()
        assert (docs_dir / ids["bob_doc"]).exists()


def test_loading_quest_docs_skill_with_gate_closed_names_the_feature_gate():
    """A closed docs gate is a feature gate, not a missing connector."""
    from chat.docs.constants import docs_disabled_message
    from chat.system_skills import load_system_skills

    [result] = load_system_skills(
        ["system:quest_docs"], {"docs": False}, "http://x", "key",
    )
    assert result["error"] == docs_disabled_message()
    assert "Data Connections" not in result["error"]
