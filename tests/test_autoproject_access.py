"""AutoProject plumbing outside the AutoProject routes.

* The feature gate (config/feature_gates.py ``FEATURE_AUTOPROJECTS``,
  ``autoprojects_enabled_for``), the connected-services pseudo-key and
  ``GET /app/api/me``'s ``enabled_features``.
* The id-to-path resolvers ``ChatStorage.get_autoproject_dir`` /
  ``get_autoproject_workspace_root``.
* Run conversations: ``ChatStorage.create_autoproject_run_conversation``
  (origin ``autoproject``, private, no unread state, hidden from the
  Conversations list), the conversation detail carrying ``autoproject_id``
  and the origin, and the generic conversation routes on a run
  conversation (rename, archive and model change behave as for any
  conversation; compaction and conversion to a project are refused).
* Inbox rows of a run's approval cards carry ``autoproject_id`` and
  ``autoproject_title``.
* Account deletion removes the AutoProject rows, the run conversations and
  every AutoProject directory.
* The SPA catch-all for ``/autoprojects/...`` and the API router mount in
  quest.py.

Uses the ``env`` fixture of tests/test_autoproject_routes.py.
"""

from __future__ import annotations

import json

import pytest

from chat.storage import ChatStorage, InvalidStorageIdError
from tests.test_autoproject_routes import (  # noqa: F401  (env is a fixture)
    _run,
    client,
    detail,
    env,
    finished_run,
    make_autoproject,
    open_action_card,
    start_run,
    uid,
)


def chat_client(env, who="alice"):
    from chat.routes import router as chat_router

    return client(env, who, chat_router)


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


class TestFeatureGate:
    @pytest.fixture()
    def fg(self, tmp_path, monkeypatch):
        import config.feature_gates as fg

        monkeypatch.setattr(fg, "FEATURE_GATES_FILE", tmp_path / "feature_gates.json")
        return fg

    def test_registered_off_by_default_per_user(self, fg):
        assert fg.FEATURE_AUTOPROJECTS == "autoprojects"
        assert fg.FEATURE_AUTOPROJECTS in fg.KNOWN_FEATURES
        assert fg.FEATURE_AUTOPROJECTS in fg.PER_USER_ACCESS_FEATURES
        assert fg.FEATURE_LABELS[fg.FEATURE_AUTOPROJECTS]["label"] == "AutoProjects"
        assert fg.FEATURE_LABELS[fg.FEATURE_AUTOPROJECTS]["description"]
        assert fg.is_feature_enabled(fg.FEATURE_AUTOPROJECTS) is False
        assert fg.autoprojects_enabled_for("a@example.com") is False
        assert fg.FEATURE_AUTOPROJECTS not in fg.enabled_features("a@example.com")

    def test_enabled_for_all_or_listed_users(self, fg):
        fg.set_feature_enabled(fg.FEATURE_AUTOPROJECTS, True)
        assert fg.autoprojects_enabled_for("a@example.com") is True
        assert fg.FEATURE_AUTOPROJECTS in fg.enabled_features("a@example.com")
        fg.set_feature_allowed_users(fg.FEATURE_AUTOPROJECTS, ["B@Example.com "])
        assert fg.autoprojects_enabled_for("b@example.com") is True
        assert fg.autoprojects_enabled_for("a@example.com") is False
        assert fg.FEATURE_AUTOPROJECTS not in fg.enabled_features("a@example.com")
        # Off again keeps the list; nobody has it.
        fg.set_feature_enabled(fg.FEATURE_AUTOPROJECTS, False)
        assert fg.autoprojects_enabled_for("b@example.com") is False

    def test_connected_services_pseudo_key_follows_the_gate(self, fg):
        from api.instructions import (
            PSEUDO_SERVICE_KEYS,
            get_public_connected_services,
            get_user_connected_services,
        )
        from chat.autoprojects.constants import AUTOPROJECTS_SERVICE_KEY

        assert AUTOPROJECTS_SERVICE_KEY in PSEUDO_SERVICE_KEYS
        user = {"id": 1, "email": "a@example.com"}
        assert get_user_connected_services(user)[AUTOPROJECTS_SERVICE_KEY] is False
        fg.set_feature_enabled(fg.FEATURE_AUTOPROJECTS, True)
        assert get_user_connected_services(user)[AUTOPROJECTS_SERVICE_KEY] is True
        # An AutoProject run is never public.
        assert get_public_connected_services(user)[AUTOPROJECTS_SERVICE_KEY] is False

    def test_me_reports_enabled_features(self, env):
        resp = chat_client(env).get("/app/api/me")
        assert resp.status_code == 200, resp.text
        assert "autoprojects" in resp.json()["enabled_features"]
        env.fg.set_feature_enabled(env.fg.FEATURE_AUTOPROJECTS, False)
        resp = chat_client(env).get("/app/api/me")
        assert "autoprojects" not in resp.json()["enabled_features"]


# ---------------------------------------------------------------------------
# Path resolvers
# ---------------------------------------------------------------------------


class TestResolvers:
    def test_canonical_id(self, env):
        ap_dir = ChatStorage.get_autoproject_dir("abc-123")
        assert ap_dir == (env.dirs["autoprojects"] / "abc-123").resolve()
        assert ChatStorage.get_autoproject_workspace_root("abc-123") == ap_dir / "workspace"
        # Nothing is created by resolving.
        assert not ap_dir.exists()

    @pytest.mark.parametrize("bad", [
        "", ".", "..", "a/b", "../x", "/abs", "x/../y", None, 5,
    ])
    def test_invalid_ids_raise(self, env, bad):
        with pytest.raises(InvalidStorageIdError):
            ChatStorage.get_autoproject_dir(bad)
        with pytest.raises(InvalidStorageIdError):
            ChatStorage.get_autoproject_workspace_root(bad)

    def test_symlinked_autoproject_dir_outside_refused(self, env, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (env.dirs["autoprojects"] / "planted").symlink_to(outside)
        with pytest.raises(InvalidStorageIdError):
            ChatStorage.get_autoproject_dir("planted")

    def test_workspace_symlinked_into_another_autoproject_refused(self, env):
        victim = env.dirs["autoprojects"] / "victim" / "workspace"
        victim.mkdir(parents=True)
        attacker = env.dirs["autoprojects"] / "attacker"
        attacker.mkdir()
        (attacker / "workspace").symlink_to(victim)
        with pytest.raises(InvalidStorageIdError):
            ChatStorage.get_autoproject_workspace_root("attacker")
        assert ChatStorage.get_autoproject_workspace_root("victim") == victim.resolve()


# ---------------------------------------------------------------------------
# Run conversations
# ---------------------------------------------------------------------------


class TestRunConversations:
    def test_create_autoproject_run_conversation(self, env):
        from db import conversation_store

        autoproject = make_autoproject(env)
        cid = _run(ChatStorage.create_autoproject_run_conversation(
            uid(env), autoproject["id"], model="gemini-3.1-pro-preview",
        ))
        history = json.loads(ChatStorage._get_chat_history_file(cid).read_text())
        assert history["origin"] == "autoproject"
        assert history["autoproject_id"] == autoproject["id"]
        assert history["messages"] == []
        meta = _run(conversation_store.get_conversation_meta(uid(env), cid))
        assert (meta["origin"], meta["autoproject_id"], meta["project_id"]) == (
            "autoproject", autoproject["id"], None,
        )
        assert meta["model"] == "gemini-3.1-pro-preview"

    def test_hidden_from_the_conversation_list_and_unread(self, env):
        from db import conversation_store

        autoproject = make_autoproject(env)
        run = finished_run(env, autoproject)
        plain, _ = _run(ChatStorage.create_conversation(uid(env)))
        assert "autoproject" not in conversation_store.UNREAD_ORIGINS
        listed = [c["id"] for c in _run(conversation_store.list_conversations_meta(uid(env)))]
        assert listed == [plain]
        everything = _run(conversation_store.list_conversations_meta(
            uid(env), include_autoproject_runs=True,
        ))
        assert {c["id"] for c in everything} == {plain, run["conversation_id"]}
        resp = chat_client(env).get("/app/api/conversations")
        assert resp.status_code == 200
        assert run["conversation_id"] not in resp.text

    def test_detail_carries_autoproject_id_and_origin(self, env):
        autoproject = make_autoproject(env)
        run = finished_run(env, autoproject)
        resp = chat_client(env).get(f"/app/api/conversations/{run['conversation_id']}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["autoproject_id"] == autoproject["id"]
        assert data["origin"] == "autoproject"
        assert data.get("project_id") is None
        # Owner only, like any conversation.
        foreign = chat_client(env, "bob").get(
            f"/app/api/conversations/{run['conversation_id']}",
        )
        assert foreign.status_code == 404
        plain, _ = _run(ChatStorage.create_conversation(uid(env)))
        assert "autoproject_id" not in chat_client(env).get(
            f"/app/api/conversations/{plain}",
        ).json()

    def test_compaction_refused(self, env):
        autoproject = make_autoproject(env)
        run = finished_run(env, autoproject)
        resp = chat_client(env).post(f"/app/api/conversations/{run['conversation_id']}/compact")
        assert resp.status_code == 409
        assert detail(resp)["error"] == "autoproject_run_conversation"

    def test_conversion_to_a_project_refused(self, env):
        from chat.project_routes import router as project_router
        from db import conversation_store, project_store

        autoproject = make_autoproject(env)
        run = finished_run(env, autoproject)
        resp = client(env, "alice", project_router).post(
            "/app/api/projects/from-conversation",
            json={"name": "P", "conversation_id": run["conversation_id"]},
        )
        assert resp.status_code == 400, resp.text
        assert detail(resp)["error"] == "autoproject_run_conversation"
        assert _run(project_store.list_projects(uid(env))) == []
        meta = _run(conversation_store.get_conversation_meta(uid(env), run["conversation_id"]))
        assert meta["project_id"] is None

    def test_generic_mutations_leave_the_autoproject_alone(self, env):
        """Rename, archive and model change are the ordinary conversation
        routes: they work on a run conversation and change nothing an
        AutoProject run inherits (title, runs)."""
        from db import conversation_store

        autoproject = make_autoproject(env)
        run = finished_run(env, autoproject)
        cid = run["conversation_id"]
        c = chat_client(env)
        assert c.put(f"/app/api/conversations/{cid}/rename",
                     json={"custom_name": "Renamed run"}).status_code == 200
        assert c.patch(f"/app/api/conversations/{cid}/model",
                       json={"model": "gemini-3.1-pro-preview"}).status_code == 200
        assert c.put(f"/app/api/conversations/{cid}/archive").status_code == 200
        meta = _run(conversation_store.get_conversation_meta(uid(env), cid))
        assert meta["autoproject_id"] == autoproject["id"]
        after = _run(env.store.get_autoproject(uid(env), autoproject["id"]))
        assert after["title"] == autoproject["title"]
        assert _run(env.store.get_run(run["id"]))["status"] == "completed"
        # Bob cannot touch it.
        assert chat_client(env, "bob").put(
            f"/app/api/conversations/{cid}/rename", json={"custom_name": "x"},
        ).status_code == 404


# ---------------------------------------------------------------------------
# Inbox rows
# ---------------------------------------------------------------------------


def test_inbox_rows_name_the_autoproject(env):
    from db import action_request_store

    autoproject = make_autoproject(env, title="Q3 launch")
    run = start_run(env, autoproject)
    request, _handle, _row = open_action_card(env, run)
    plain, _ = _run(ChatStorage.create_conversation(uid(env)))
    plain_request = _run(action_request_store.create_action_request(
        uid(env), plain, "create_memory", {"content": "y"}, "why",
    ))
    rows = {r["id"]: r for r in _run(action_request_store.list_action_requests_enriched(uid(env)))}
    assert (rows[request["id"]]["autoproject_id"], rows[request["id"]]["autoproject_title"]) == (
        autoproject["id"], "Q3 launch",
    )
    assert rows[request["id"]]["project_id"] is None
    assert (rows[plain_request["id"]]["autoproject_id"],
            rows[plain_request["id"]]["autoproject_title"]) == (None, None)


# ---------------------------------------------------------------------------
# Account deletion
# ---------------------------------------------------------------------------


def test_account_delete_removes_autoprojects_runs_and_directories(env):
    import chat.routes.user as user_routes
    from db import conversation_store

    active = make_autoproject(env, title="Active")
    archived = make_autoproject(env, title="Archived")
    runs = [finished_run(env, active), finished_run(env, archived)]
    from chat.autoprojects import service

    _run(service.set_autoproject_archived(env.users["alice"], archived["id"], True))
    for ap in (active, archived):
        root = ChatStorage.get_autoproject_workspace_root(ap["id"])
        root.mkdir(parents=True)
        (root / "f.md").write_text("x")
    bob_ap = make_autoproject(env, "bob", title="Bob's")
    bob_run = finished_run(env, bob_ap)
    ChatStorage.get_autoproject_workspace_root(bob_ap["id"]).mkdir(parents=True)

    response = _run(user_routes.delete_account(request=None, user=env.users["alice"]))
    assert response.status_code == 200

    for ap in (active, archived):
        assert _run(env.store.get_autoproject_by_id(ap["id"])) is None
        assert not (env.dirs["autoprojects"] / ap["id"]).exists()
    for run in runs:
        assert _run(env.store.get_run(run["id"])) is None
        assert not (env.dirs["chats"] / run["conversation_id"]).exists()
        assert _run(conversation_store.get_conversation_meta(
            uid(env), run["conversation_id"],
        )) is None
    # Bob's are untouched.
    assert _run(env.store.get_autoproject_by_id(bob_ap["id"])) is not None
    assert _run(env.store.get_run(bob_run["id"])) is not None
    assert (env.dirs["chats"] / bob_run["conversation_id"]).exists()
    assert (env.dirs["autoprojects"] / bob_ap["id"] / "workspace").is_dir()


# ---------------------------------------------------------------------------
# quest.py: SPA catch-all and the router mount
# ---------------------------------------------------------------------------

def test_spa_autoprojects_routes_precede_static_fallback(quest_route_table):
    routes = {tuple(r) for r in quest_route_table["routes"]}
    paths = quest_route_table["paths"]
    for path in ("/autoprojects", "/autoprojects/{rest:path}"):
        assert ("GET", path) in routes
        assert paths.index(path) < paths.index("/{filename}")


def test_autoprojects_api_mounted(quest_route_table):
    routes = {tuple(r) for r in quest_route_table["routes"]}
    for method, path in [
        ("GET", "/app/api/autoprojects"),
        ("POST", "/app/api/autoprojects"),
        ("GET", "/app/api/autoprojects/{autoproject_id}"),
        ("PATCH", "/app/api/autoprojects/{autoproject_id}"),
        ("DELETE", "/app/api/autoprojects/{autoproject_id}"),
        ("PUT", "/app/api/autoprojects/{autoproject_id}/brief"),
        ("GET", "/app/api/autoprojects/{autoproject_id}/brief/versions"),
        ("GET", "/app/api/autoprojects/{autoproject_id}/brief/versions/{version}"),
        ("POST", "/app/api/autoprojects/{autoproject_id}/brief/versions/{version}/restore"),
        ("GET", "/app/api/autoprojects/{autoproject_id}/runs"),
        ("POST", "/app/api/autoprojects/{autoproject_id}/runs"),
        ("DELETE", "/app/api/autoprojects/{autoproject_id}/runs/{run_id}"),
        ("POST", "/app/api/autoprojects/{autoproject_id}/questions/{handle_id}/answer"),
        ("POST", "/app/api/autoprojects/{autoproject_id}/read"),
        ("PUT", "/app/api/autoprojects/{autoproject_id}/archive"),
        ("PUT", "/app/api/autoprojects/{autoproject_id}/unarchive"),
        ("GET", "/app/api/autoprojects/{autoproject_id}/files"),
        ("POST", "/app/api/autoprojects/{autoproject_id}/files/upload"),
        ("POST", "/app/api/conversations/{conversation_id}/files/copy-to-autoproject"),
        ("POST", "/app/api/conversations/{conversation_id}/files/copy-from-autoproject"),
    ]:
        assert (method, path) in routes, (method, path)
