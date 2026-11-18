"""AutoProject HTTP routes (chat/autoprojects/routes.py, http.py, views.py).

Runs the router in a bare FastAPI app with the auth dependency overridden
(tests/test_table_routes.py pattern) on the ``env`` fixture below: the
shared ``isolated_db`` (every store, ``db.autoproject_store`` included,
on a temp SQLite file), temp ``CHATS_DIR`` / ``AUTOPROJECTS_DIR``, the
``autoprojects`` gate open, realtime events captured and the headless resume
recorded instead of run. Users: alice and bob. No LLM is ever called: runs
are seeded through the store the way the send path allocates them.

Covers every route of the AutoProjects API: auth, the gate (403
``autoprojects_disabled`` on every route), missing and foreign AutoProjects
answering the same 404 body, the response shapes, every error code of
the route table, caps, oversized integers (422), thread paging with
questions and open requests, the brief and its versions, run delete and
AutoProject delete with their directory sweeps, archive while a run is
active, and the read acknowledgement.

``env`` and the seeding helpers are imported by
tests/test_autoproject_file_routes.py and tests/test_autoproject_access.py.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chat.autoprojects import service
from chat.autoprojects.constants import (
    AUTOPROJECT_ANSWER_MAX_CHARS,
    AUTOPROJECT_BRIEF_MAX_CHARS,
    AUTOPROJECT_TITLE_MAX_CHARS,
    autoproject_not_found_message,
    autoprojects_disabled_message,
)
from chat.autoprojects.questions import post_question
from db.models import ToolWaitHandleKind

_MAX_INT = 2**31 - 1


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixture and helpers (shared with the other AutoProject HTTP test files)
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(isolated_db, tmp_path, monkeypatch):
    import chat.storage as storage_mod
    import config.feature_gates as fg
    from chat.realtime.bus import bus
    from chat.wait_handles import resume as wait_resume
    from db import autoproject_store

    models = isolated_db.models
    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", tmp_path / "feature_gates.json")
    fg.set_feature_enabled(fg.FEATURE_AUTOPROJECTS, True)

    dirs = {name: tmp_path / name for name in ("chats", "projects", "autoprojects")}
    for path in dirs.values():
        path.mkdir()
    monkeypatch.setattr(storage_mod, "CHATS_DIR", dirs["chats"])
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", dirs["projects"])
    monkeypatch.setattr(storage_mod, "AUTOPROJECTS_DIR", dirs["autoprojects"])

    published: list[tuple[int, dict]] = []
    monkeypatch.setattr(bus, "publish_to_user", lambda uid, ev: published.append((uid, ev)))
    monkeypatch.setattr(bus, "connected_user_ids", lambda: [])

    kicked: list[tuple[int, str]] = []

    def fake_kick(app, user, conversation_id):
        kicked.append((user["id"], conversation_id))
        return None

    monkeypatch.setattr(wait_resume, "maybe_kick_resume", fake_kick)

    async def _seed():
        async with isolated_db.AsyncSessionLocal() as db:
            users = [
                models.User(email=f"{name}@example.com", api_key=f"k-{uuid.uuid4().hex}")
                for name in ("alice", "bob")
            ]
            db.add_all(users)
            await db.commit()
            for u in users:
                await db.refresh(u)
            return {u.email.split("@")[0]: {"id": u.id, "email": u.email} for u in users}

    return SimpleNamespace(
        db=isolated_db, users=_run(_seed()), dirs=dirs, published=published,
        kicked=kicked, fg=fg, store=autoproject_store, models=models,
    )


def make_app(env, who="alice", *routers):
    """A bare app with ``routers`` (default: the AutoProject routes), the auth
    dependency returning ``env.users[who]`` (anonymous for None)."""
    from chat.autoprojects import routes
    from chat.auth import get_current_user_cookie_or_apikey_checked

    app = FastAPI()
    for router in routers or (routes.router,):
        app.include_router(router)
    if who is not None:
        user = env.users[who] if isinstance(who, str) else who

        async def _current():
            return user

        app.dependency_overrides[get_current_user_cookie_or_apikey_checked] = _current
    return app


def client(env, who="alice", *routers):
    return TestClient(make_app(env, who, *routers))


def uid(env, who="alice"):
    return env.users[who]["id"]


def make_autoproject(env, who="alice", title="Launch plan", **kwargs):
    autoproject = _run(service.create_autoproject(env.users[who], title=title, **kwargs))
    env.published.clear()
    return autoproject


def start_run(env, autoproject, prompt="Do the next step"):
    """A run the way the send path starts one: its conversation (owned by
    the AutoProject's owner), then the run row (``running``)."""
    from chat.storage import ChatStorage

    conversation_id = _run(ChatStorage.create_autoproject_run_conversation(
        autoproject["user_id"], autoproject["id"],
    ))
    run = _run(env.store.allocate_run(
        autoproject["id"], conversation_id=conversation_id, prompt=prompt,
    ))
    env.published.clear()
    return run


def finish_run(env, run, *, summary="Did it", result="The result.", notes="Notes."):
    done = _run(env.store.complete_run(run["id"], summary=summary, result=result, notes=notes))
    env.published.clear()
    return done


def finished_run(env, autoproject, prompt="Do the next step", **entry):
    return finish_run(env, start_run(env, autoproject, prompt), **entry)


def set_waiting(env, run):
    return _run(env.store.set_run_status(run["id"], "waiting"))


def ask(env, run, question="Which colour?", options=("Red", "Blue"), who="alice"):
    """A pending ``autoproject_question`` handle on ``run`` and its transcript
    message, as ``ask_autoproject_question`` leaves them."""
    from chat.autoprojects.questions import make_question_message

    handle = _run(post_question(
        user_id=uid(env, who), conversation_id=run["conversation_id"], run_id=run["id"],
        tool_id=f"tool-{uuid.uuid4().hex[:8]}", question=question, options=list(options),
    ))
    append_transcript(run["conversation_id"], make_question_message(handle))
    return handle


def append_transcript(conversation_id, *messages):
    from chat.storage import ChatStorage

    path = ChatStorage._get_chat_history_file(conversation_id)
    data = json.loads(path.read_text())
    data["messages"].extend(messages)
    path.write_text(json.dumps(data))


def read_transcript(conversation_id):
    from chat.storage import ChatStorage

    return json.loads(ChatStorage._get_chat_history_file(conversation_id).read_text())


def open_action_card(env, run, who="alice"):
    """An open approval card on ``run``: the action request, its pending
    wait handle and the transcript row the AutoProject page shows."""
    from db import action_request_store, tool_wait_handle_store

    request = _run(action_request_store.create_action_request(
        uid(env, who), run["conversation_id"], "create_memory", {"content": "x"}, "why",
    ))
    handle = _run(tool_wait_handle_store.create_handle(
        user_id=uid(env, who), conversation_id=run["conversation_id"],
        kind=ToolWaitHandleKind.ACTION_REQUEST.value, tool_id=f"t-{request['id']}",
        payload={"action_request_id": request["id"]},
        correlation_kind="action_request", correlation_id=str(request["id"]),
    ))
    row = {
        "type": "action_request", "wait_handle_id": handle["id"],
        "action_request_id": request["id"], "status": "open",
    }
    append_transcript(run["conversation_id"], {"type": "text", "content": "before"}, row)
    return request, handle, row


def detail(resp):
    return resp.json()["detail"]


def event_types(env):
    return [ev["type"] for _uid, ev in env.published]


def not_found_body(autoproject_id):
    return {"detail": {
        "error": "autoproject_not_found",
        "message": autoproject_not_found_message(autoproject_id),
    }}


DISABLED_BODY = {"detail": {
    "error": "autoprojects_disabled", "message": autoprojects_disabled_message(),
}}

DETAIL_KEYS = {
    "id", "title", "title_is_default", "archived", "brief", "brief_version",
    "brief_updated_at", "brief_source", "brief_run_seq", "model", "thinking_effort",
    "run_count", "active_run", "event_seq", "unread_seq", "read_seq", "unread",
    "created_at", "updated_at", "last_activity_at",
}
LIST_ROW_KEYS = {
    "id", "title", "archived", "last_activity_at", "active_run", "unread",
    "event_seq", "unread_seq", "read_seq",
}
RUN_VIEW_KEYS = {
    "id", "seq", "conversation_id", "prompt", "status", "summary", "result", "notes",
    "handoff_missing", "error", "model", "started_at", "finished_at", "questions",
    "open_requests",
}

# Every /autoprojects/{id}... route of chat/autoprojects/routes.py with a body
# that would pass validation. ``{rid}`` / ``{hid}`` are filled per test.
PER_ID_ROUTES = [
    ("GET", "/app/api/autoprojects/{id}", None),
    ("PATCH", "/app/api/autoprojects/{id}", {"title": "Renamed"}),
    ("PUT", "/app/api/autoprojects/{id}/brief", {"brief": "x", "expected_brief_version": 0}),
    ("GET", "/app/api/autoprojects/{id}/brief/versions", None),
    ("GET", "/app/api/autoprojects/{id}/brief/versions/1", None),
    ("POST", "/app/api/autoprojects/{id}/brief/versions/1/restore",
     {"expected_brief_version": 1}),
    ("GET", "/app/api/autoprojects/{id}/runs", None),
    ("POST", "/app/api/autoprojects/{id}/runs", None),
    ("DELETE", "/app/api/autoprojects/{id}/runs/{rid}", None),
    ("POST", "/app/api/autoprojects/{id}/questions/{hid}/answer", {"answer": "Red"}),
    ("POST", "/app/api/autoprojects/{id}/read", {"seq": 1}),
    ("PUT", "/app/api/autoprojects/{id}/archive", None),
    ("PUT", "/app/api/autoprojects/{id}/unarchive", None),
    ("DELETE", "/app/api/autoprojects/{id}", None),
]
COLLECTION_ROUTES = [
    ("GET", "/app/api/autoprojects", None),
    ("GET", "/app/api/autoprojects?archived=true", None),
    ("POST", "/app/api/autoprojects", {"title": "New"}),
]


def _request(c, method, url, payload):
    kwargs = {"json": payload} if payload is not None else {}
    return c.request(method, url, **kwargs)


# ---------------------------------------------------------------------------
# Auth, gate, not-found parity
# ---------------------------------------------------------------------------


class TestAuthAndGate:
    def test_anonymous_401(self, env):
        resp = client(env, None).get("/app/api/autoprojects")
        assert resp.status_code == 401

    def test_every_route_403s_before_any_db_work(self, env, monkeypatch):
        autoproject = make_autoproject(env)
        run = finished_run(env, autoproject)
        env.fg.set_feature_enabled(env.fg.FEATURE_AUTOPROJECTS, False)

        async def boom(*_a, **_k):
            raise AssertionError("DB touched with the gate closed")

        for name in ("get_autoproject", "list_autoprojects", "create_autoproject",
                     "delete_run", "delete_autoproject", "set_autoproject_archived"):
            monkeypatch.setattr(env.store, name, boom)
        c = client(env)
        for method, url, payload in COLLECTION_ROUTES + PER_ID_ROUTES:
            url = url.format(id=autoproject["id"], rid=run["id"], hid="h")
            resp = _request(c, method, url, payload)
            assert resp.status_code == 403, (method, url, resp.text)
            assert resp.json() == DISABLED_BODY, (method, url)
        assert env.published == []
        assert env.kicked == []

    def test_gate_per_user_access_list(self, env):
        make_autoproject(env, "alice")
        env.fg.set_feature_allowed_users(env.fg.FEATURE_AUTOPROJECTS, ["Bob@Example.com"])
        assert client(env, "alice").get("/app/api/autoprojects").status_code == 403
        assert client(env, "bob").get("/app/api/autoprojects").status_code == 200

    def test_reopening_the_gate_restores_everything(self, env):
        autoproject = make_autoproject(env)
        finished_run(env, autoproject)
        env.fg.set_feature_enabled(env.fg.FEATURE_AUTOPROJECTS, False)
        assert client(env).get("/app/api/autoprojects").status_code == 403
        env.fg.set_feature_enabled(env.fg.FEATURE_AUTOPROJECTS, True)
        c = client(env)
        assert [r["id"] for r in c.get("/app/api/autoprojects").json()["autoprojects"]] == [
            autoproject["id"]
        ]
        assert len(c.get(f"/app/api/autoprojects/{autoproject['id']}/runs").json()["runs"]) == 1


class TestNotFoundParity:
    @pytest.mark.parametrize(
        "method,url,payload", PER_ID_ROUTES, ids=[f"{m} {u}" for m, u, _ in PER_ID_ROUTES],
    )
    def test_foreign_equals_missing(self, env, method, url, payload):
        secret = make_autoproject(env, "bob", title="Bob's secret")
        bob_run = finished_run(env, secret)
        _run(service.set_brief(env.users["bob"], secret["id"], "Bob's brief",
                               expected_brief_version=0))
        env.published.clear()
        alice = client(env, "alice")

        foreign = _request(
            alice, method, url.format(id=secret["id"], rid=bob_run["id"], hid="h"), payload,
        )
        assert foreign.status_code == 404, foreign.text
        assert foreign.json() == not_found_body(secret["id"])
        assert b"secret" not in foreign.content and b"Bob" not in foreign.content

        missing_id = str(uuid.uuid4())
        missing = _request(
            alice, method, url.format(id=missing_id, rid=bob_run["id"], hid="h"), payload,
        )
        assert missing.status_code == 404
        assert missing.content == foreign.content.replace(
            secret["id"].encode(), missing_id.encode(),
        )

        # Bob's AutoProject is untouched and nothing was announced.
        after = _run(env.store.get_autoproject(uid(env, "bob"), secret["id"]))
        assert after["title"] == "Bob's secret"
        assert after["archived"] is False
        assert after["brief"] == "Bob's brief"
        assert _run(env.store.get_run(bob_run["id"])) is not None
        assert env.published == [] and env.kicked == []


# ---------------------------------------------------------------------------
# AutoProjects: list, create, get, patch
# ---------------------------------------------------------------------------


class TestList:
    def test_rows_newest_activity_first_own_only(self, env):
        first = make_autoproject(env, title="First")
        second = make_autoproject(env, title="Second")
        make_autoproject(env, "bob", title="Bob's")
        # A run moves last_activity_at: the first AutoProject comes back on top.
        run = start_run(env, first)
        resp = client(env).get("/app/api/autoprojects")
        assert resp.status_code == 200
        rows = resp.json()["autoprojects"]
        assert [r["id"] for r in rows] == [first["id"], second["id"]]
        assert all(set(r) == LIST_ROW_KEYS for r in rows)
        assert rows[0]["active_run"] == {"id": run["id"], "seq": 1, "status": "running"}
        assert rows[1]["active_run"] is None
        assert rows[0]["unread"] is False

    def test_archived_filter(self, env):
        kept = make_autoproject(env, title="Kept")
        gone = make_autoproject(env, title="Gone")
        c = client(env)
        assert c.put(f"/app/api/autoprojects/{gone['id']}/archive").status_code == 200
        assert [r["id"] for r in c.get("/app/api/autoprojects").json()["autoprojects"]] == [
            kept["id"]
        ]
        archived = c.get("/app/api/autoprojects", params={"archived": "true"}).json()
        assert [(r["id"], r["archived"]) for r in archived["autoprojects"]] == [
            (gone["id"], True)
        ]

    def test_bad_archived_value_422(self, env):
        assert client(env).get("/app/api/autoprojects?archived=maybe").status_code == 422


class TestCreate:
    @pytest.mark.parametrize("body,title,is_default", [
        ({"title": "  My   plan  "}, "My plan", False),
        ({"prompt": "\n  Write the launch\nplan for Q3  "}, "Write the launch", True),
        ({"prompt": "x" * 500}, "x" * (AUTOPROJECT_TITLE_MAX_CHARS - 1) + "…", True),
        ({}, "Untitled AutoProject", True),
        # An explicit title wins over the prompt.
        ({"title": "Named", "prompt": "Cut from here"}, "Named", False),
    ])
    def test_title_sources(self, env, body, title, is_default):
        resp = client(env).post("/app/api/autoprojects", json=body)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert set(data) == DETAIL_KEYS
        assert (data["title"], data["title_is_default"]) == (title, is_default)
        assert data["brief"] == "" and data["brief_version"] == 0
        assert data["run_count"] == 0 and data["active_run"] is None
        assert data["archived"] is False
        assert event_types(env) == ["autoproject_list_changed"]
        assert env.published[0][0] == uid(env)

    def test_prompt_is_not_stored_as_a_run(self, env):
        resp = client(env).post("/app/api/autoprojects", json={"prompt": "Start here"})
        assert _run(env.store.count_runs(resp.json()["id"])) == 0

    def test_model_and_thinking_effort(self, env):
        resp = client(env).post(
            "/app/api/autoprojects",
            json={"title": "T", "model": " gemini-3.1-pro-preview ", "thinking_effort": "high"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["model"] == "gemini-3.1-pro-preview"
        assert resp.json()["thinking_effort"] == "high"

    @pytest.mark.parametrize("body,code", [
        ({"title": "   "}, "invalid_title"),
        ({"title": "x" * (AUTOPROJECT_TITLE_MAX_CHARS + 1)}, "invalid_title"),
        ({"title": "T", "model": "   "}, "invalid_model"),
        ({"title": "T", "model": "m" * 101}, "invalid_model"),
        ({"title": "T", "thinking_effort": ""}, "invalid_thinking_effort"),
    ])
    def test_validation_400(self, env, body, code):
        resp = client(env).post("/app/api/autoprojects", json=body)
        assert resp.status_code == 400, resp.text
        assert detail(resp)["error"] == code
        assert _run(env.store.list_autoprojects(uid(env))) == []
        assert env.published == []

    def test_model_not_allowed_400(self, env, monkeypatch):
        import config.model_selection as model_selection

        monkeypatch.setattr(model_selection, "is_model_allowed", lambda m, public: False)
        resp = client(env).post("/app/api/autoprojects", json={"title": "T", "model": "m1"})
        assert resp.status_code == 400
        assert detail(resp)["error"] == "model_not_allowed"
        assert detail(resp)["model"] == "m1"

    @pytest.mark.parametrize("body", [
        {"title": 5}, {"prompt": ["a"]}, {"model": {"a": 1}},
    ])
    def test_ill_typed_body_422(self, env, body):
        assert client(env).post("/app/api/autoprojects", json=body).status_code == 422

    def test_cap_reached_409(self, env, monkeypatch):
        monkeypatch.setattr(env.store, "MAX_ACTIVE_AUTOPROJECTS_PER_USER", 2)
        make_autoproject(env, title="One")
        archived = make_autoproject(env, title="Two")
        c = client(env)
        resp = c.post("/app/api/autoprojects", json={"title": "Three"})
        assert resp.status_code == 409
        assert detail(resp)["error"] == "autoprojects_cap_reached"
        assert len(_run(env.store.list_autoprojects(uid(env)))) == 2
        # Archived AutoProjects do not count.
        c.put(f"/app/api/autoprojects/{archived['id']}/archive")
        assert c.post("/app/api/autoprojects", json={"title": "Three"}).status_code == 201
        # Another user's AutoProjects do not count either.
        assert client(env, "bob").post(
            "/app/api/autoprojects", json={"title": "B"},
        ).status_code == 201


class TestGetAndPatch:
    def test_detail(self, env):
        autoproject = make_autoproject(env)
        finished_run(env, autoproject)
        run = start_run(env, autoproject)
        set_waiting(env, run)
        resp = client(env).get(f"/app/api/autoprojects/{autoproject['id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data) == DETAIL_KEYS
        assert data["run_count"] == 2
        assert data["active_run"] == {"id": run["id"], "seq": 2, "status": "waiting"}
        assert (data["brief_source"], data["brief_run_seq"]) == (None, None)
        # Store internals never leave the views.
        assert "user_id" not in data and "next_run_seq" not in data

    def test_brief_authorship_names_the_run(self, env):
        autoproject = make_autoproject(env)
        run = finished_run(env, autoproject)
        _run(env.store.set_brief(
            autoproject["id"], "From run 1", expected_brief_version=0, source=f"run:{run['id']}",
        ))
        data = client(env).get(f"/app/api/autoprojects/{autoproject['id']}").json()
        assert (data["brief_source"], data["brief_run_seq"]) == (f"run:{run['id']}", 1)

    def test_patch_title_clears_default_and_publishes(self, env):
        autoproject = _run(service.create_autoproject(env.users["alice"], prompt="Cut me"))
        assert autoproject["title_is_default"] is True
        env.published.clear()
        resp = client(env).patch(
            f"/app/api/autoprojects/{autoproject['id']}", json={"title": " New  name "},
        )
        assert resp.status_code == 200, resp.text
        assert (resp.json()["title"], resp.json()["title_is_default"]) == ("New name", False)
        assert set(resp.json()) == DETAIL_KEYS
        assert event_types(env) == ["autoproject_changed", "autoproject_list_changed"]

    def test_patch_absent_fields_stay_null_clears(self, env):
        autoproject = make_autoproject(
            env, model="gemini-3.1-pro-preview", thinking_effort="high",
        )
        c = client(env)
        url = f"/app/api/autoprojects/{autoproject['id']}"
        data = c.patch(url, json={"thinking_effort": None}).json()
        assert (data["title"], data["model"], data["thinking_effort"]) == (
            "Launch plan", "gemini-3.1-pro-preview", None,
        )
        data = c.patch(url, json={"model": None}).json()
        assert data["model"] is None

    def test_patch_without_change_publishes_nothing(self, env):
        autoproject = make_autoproject(env)
        resp = client(env).patch(f"/app/api/autoprojects/{autoproject['id']}", json={})
        assert resp.status_code == 200
        assert env.published == []

    @pytest.mark.parametrize("body,code", [
        ({"title": None}, "invalid_title"),
        ({"title": ""}, "invalid_title"),
        ({"title": "y" * (AUTOPROJECT_TITLE_MAX_CHARS + 1)}, "invalid_title"),
        ({"model": ""}, "invalid_model"),
        ({"thinking_effort": "z" * 21}, "invalid_thinking_effort"),
    ])
    def test_patch_validation_400(self, env, body, code):
        autoproject = make_autoproject(env)
        resp = client(env).patch(f"/app/api/autoprojects/{autoproject['id']}", json=body)
        assert resp.status_code == 400
        assert detail(resp)["error"] == code
        assert _run(env.store.get_autoproject(uid(env), autoproject["id"]))["title"] == (
            "Launch plan"
        )


# ---------------------------------------------------------------------------
# Brief and versions
# ---------------------------------------------------------------------------


class TestBrief:
    def _url(self, autoproject, suffix=""):
        return f"/app/api/autoprojects/{autoproject['id']}/brief{suffix}"

    def test_save_returns_state_and_publishes(self, env):
        autoproject = make_autoproject(env)
        resp = client(env).put(
            self._url(autoproject), json={"brief": "  Facts\n", "expected_brief_version": 0},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert set(data) == {
            "brief", "brief_version", "brief_updated_at", "brief_source", "brief_run_seq",
        }
        assert (data["brief"], data["brief_version"]) == ("Facts", 1)
        assert (data["brief_source"], data["brief_run_seq"]) == ("ui", None)
        assert data["brief_updated_at"]
        # The page changed, the list row did not.
        stored = _run(env.store.get_autoproject(uid(env), autoproject["id"]))
        assert env.published == [(uid(env), {
            "type": "autoproject_changed", "autoproject_id": autoproject["id"],
            "event_seq": stored["event_seq"], "brief_version": 1, "active_run": None,
        })]

    def test_stale_409_carries_current(self, env):
        autoproject = make_autoproject(env)
        c = client(env)
        c.put(self._url(autoproject), json={"brief": "Tab one", "expected_brief_version": 0})
        env.published.clear()
        resp = c.put(self._url(autoproject), json={"brief": "Tab two", "expected_brief_version": 0})
        assert resp.status_code == 409
        body = detail(resp)
        assert body["error"] == "stale_brief"
        assert body["current"]["brief"] == "Tab one"
        assert body["current"]["brief_version"] == 1
        assert env.published == []
        assert _run(env.store.get_autoproject(uid(env), autoproject["id"]))["brief"] == "Tab one"

    def test_allowed_while_a_run_is_active(self, env):
        autoproject = make_autoproject(env)
        start_run(env, autoproject)
        resp = client(env).put(
            self._url(autoproject), json={"brief": "Mid-run edit", "expected_brief_version": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["brief_version"] == 1

    def test_empty_brief_allowed(self, env):
        autoproject = make_autoproject(env)
        resp = client(env).put(
            self._url(autoproject), json={"brief": "   ", "expected_brief_version": 0},
        )
        assert resp.status_code == 200 and resp.json()["brief"] == ""

    def test_too_long_400_with_cap(self, env):
        autoproject = make_autoproject(env)
        resp = client(env).put(self._url(autoproject), json={
            "brief": "b" * (AUTOPROJECT_BRIEF_MAX_CHARS + 1), "expected_brief_version": 0,
        })
        assert resp.status_code == 400
        assert detail(resp)["error"] == "brief_too_long"
        assert detail(resp)["cap"] == AUTOPROJECT_BRIEF_MAX_CHARS
        assert detail(resp)["actual"] == AUTOPROJECT_BRIEF_MAX_CHARS + 1

    @pytest.mark.parametrize("body", [
        {"brief": "x"},
        {"brief": "x", "expected_brief_version": "0"},
        {"brief": "x", "expected_brief_version": -1},
        {"brief": "x", "expected_brief_version": 1.0},
        {"brief": "x", "expected_brief_version": True},
        {"brief": "x", "expected_brief_version": _MAX_INT + 1},
        {"brief": 3, "expected_brief_version": 0},
        {"expected_brief_version": 0},
    ])
    def test_ill_typed_422(self, env, body):
        autoproject = make_autoproject(env)
        assert client(env).put(self._url(autoproject), json=body).status_code == 422

    def test_versions_list_get_restore(self, env):
        autoproject = make_autoproject(env)
        run = finished_run(env, autoproject)
        c = client(env)
        c.put(self._url(autoproject), json={"brief": "One " * 100, "expected_brief_version": 0})
        _run(env.store.set_brief(
            autoproject["id"], "Two", expected_brief_version=1, source=f"run:{run['id']}",
        ))

        versions = c.get(self._url(autoproject, "/versions")).json()["versions"]
        assert [(v["version"], v["source"], v["run_seq"]) for v in versions] == [
            (2, f"run:{run['id']}", 1), (1, "ui", None),
        ]
        assert all(set(v) == {"version", "source", "run_seq", "created_at", "excerpt"}
                   for v in versions)
        assert versions[1]["excerpt"].endswith("…") and len(versions[1]["excerpt"]) <= 200

        one = c.get(self._url(autoproject, "/versions/1")).json()
        assert set(one) == {"version", "source", "run_seq", "created_at", "content"}
        assert one["content"] == ("One " * 100).strip()

        env.published.clear()
        resp = c.post(self._url(autoproject, "/versions/1/restore"),
                      json={"expected_brief_version": 2})
        assert resp.status_code == 200, resp.text
        assert resp.json()["brief_version"] == 3
        assert resp.json()["brief"] == ("One " * 100).strip()
        assert resp.json()["brief_source"] == "restore:1"
        assert event_types(env) == ["autoproject_changed"]
        assert c.get(self._url(autoproject, "/versions")).json()["versions"][0]["source"] == (
            "restore:1"
        )

    def test_restore_stale_409_and_missing_version_404(self, env):
        autoproject = make_autoproject(env)
        c = client(env)
        c.put(self._url(autoproject), json={"brief": "v1", "expected_brief_version": 0})
        c.put(self._url(autoproject), json={"brief": "v2", "expected_brief_version": 1})
        resp = c.post(self._url(autoproject, "/versions/1/restore"),
                      json={"expected_brief_version": 1})
        assert resp.status_code == 409 and detail(resp)["error"] == "stale_brief"
        assert detail(resp)["current"]["brief"] == "v2"
        for url, method, payload in (
            (self._url(autoproject, "/versions/9"), "GET", None),
            (self._url(autoproject, "/versions/0"), "GET", None),
            (self._url(autoproject, "/versions/-1"), "GET", None),
            (self._url(autoproject, "/versions/9/restore"), "POST", {"expected_brief_version": 2}),
        ):
            resp = _request(c, method, url, payload)
            assert resp.status_code == 404, url
            assert detail(resp)["error"] == "brief_version_not_found"


# ---------------------------------------------------------------------------
# Runs: the page, start, delete
# ---------------------------------------------------------------------------


class TestRunsPage:
    def _page(self, env, autoproject, **params):
        resp = client(env).get(f"/app/api/autoprojects/{autoproject['id']}/runs", params=params)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_keyset_paging_newest_first(self, env):
        autoproject = make_autoproject(env)
        for n in range(5):
            finished_run(env, autoproject, prompt=f"Prompt {n + 1}")
        page = self._page(env, autoproject, limit=2)
        assert [r["seq"] for r in page["runs"]] == [5, 4]
        assert page["next_before_seq"] == 4
        page = self._page(env, autoproject, limit=2, before_seq=4)
        assert [r["seq"] for r in page["runs"]] == [3, 2]
        page = self._page(env, autoproject, limit=2, before_seq=2)
        assert [r["seq"] for r in page["runs"]] == [1]
        assert page["next_before_seq"] is None
        assert self._page(env, autoproject)["next_before_seq"] is None
        assert len(self._page(env, autoproject)["runs"]) == 5

    def test_empty_autoproject(self, env):
        assert self._page(env, make_autoproject(env)) == {"runs": [], "next_before_seq": None}

    @pytest.mark.parametrize("params", [
        {"limit": 0}, {"limit": 101}, {"before_seq": 0}, {"before_seq": _MAX_INT + 1},
        {"before_seq": "x"}, {"limit": 2**63},
    ])
    def test_bad_paging_params_422(self, env, params):
        autoproject = make_autoproject(env)
        resp = client(env).get(f"/app/api/autoprojects/{autoproject['id']}/runs", params=params)
        assert resp.status_code == 422

    def test_run_view_shape_and_finished_entry(self, env):
        autoproject = make_autoproject(env)
        run = finished_run(env, autoproject, prompt="Do X",
                           summary="Did X", result="X is done.", notes="Look at y.")
        [view] = self._page(env, autoproject)["runs"]
        assert set(view) == RUN_VIEW_KEYS
        assert view["id"] == run["id"] and view["conversation_id"] == run["conversation_id"]
        assert (view["prompt"], view["status"]) == ("Do X", "completed")
        assert (view["summary"], view["result"], view["notes"]) == (
            "Did X", "X is done.", "Look at y.",
        )
        assert view["handoff_missing"] is False and view["finished_at"]
        assert view["questions"] == [] and view["open_requests"] == []

    def test_active_run_hides_its_pending_entry(self, env):
        autoproject = make_autoproject(env)
        run = start_run(env, autoproject)
        # A brief conflict saved the entry without finishing the run.
        _run(env.store.save_run_entry(run["id"], summary="S", result="R", notes="N"))
        [view] = self._page(env, autoproject)["runs"]
        assert view["status"] == "running"
        assert (view["summary"], view["result"], view["notes"]) == (None, None, None)

    def test_failed_and_stopped_runs(self, env):
        autoproject = make_autoproject(env)
        failed = start_run(env, autoproject)
        _run(env.store.set_run_status(failed["id"], "failed", error="Provider error"))
        stopped = start_run(env, autoproject)
        _run(env.store.set_run_status(stopped["id"], "stopped"))
        views = self._page(env, autoproject)["runs"]
        assert [(v["seq"], v["status"], v["error"]) for v in views] == [
            (2, "stopped", None), (1, "failed", "Provider error"),
        ]

    def test_questions_on_runs(self, env):
        autoproject = make_autoproject(env)
        older = start_run(env, autoproject)
        answered = ask(env, older, "First?", options=())
        _run(service.answer_question(
            env.users["alice"], autoproject["id"], answered["id"], "Yes", app=None,
        ))
        finish_run(env, older)
        run = start_run(env, autoproject)
        set_waiting(env, run)
        pending = ask(env, run, "Which colour?", options=("Red", "Blue"))

        views = {v["seq"]: v for v in self._page(env, autoproject)["runs"]}
        [q] = views[2]["questions"]
        assert set(q) == {
            "handle_id", "question", "options", "answer", "status", "asked_at", "answered_at",
        }
        assert (q["handle_id"], q["question"], q["options"], q["answer"], q["status"]) == (
            pending["id"], "Which colour?", ["Red", "Blue"], None, "pending",
        )
        [a] = views[1]["questions"]
        assert (a["status"], a["answer"]) == ("answered", "Yes")
        assert a["answered_at"]

    def test_open_requests_of_active_runs_only(self, env):
        autoproject = make_autoproject(env)
        finished = start_run(env, autoproject)
        open_action_card(env, finished)
        finish_run(env, finished)
        run = start_run(env, autoproject)
        set_waiting(env, run)
        _request_row, handle, row = open_action_card(env, run)

        views = {v["seq"]: v for v in self._page(env, autoproject)["runs"]}
        assert views[2]["open_requests"] == [row]
        # A finished run shows no cards even if a row is somehow left open.
        assert views[1]["open_requests"] == []

        # A resolved card drops off the page.
        from db import tool_wait_handle_store

        _run(tool_wait_handle_store.resolve_handle(
            handle["id"], new_status="accepted", response={}, user_id=uid(env),
        ))
        views = {v["seq"]: v for v in self._page(env, autoproject)["runs"]}
        assert views[2]["open_requests"] == []


class TestCreateRunConversation:
    def _post(self, env, autoproject, who="alice"):
        return client(env, who).post(f"/app/api/autoprojects/{autoproject['id']}/runs")

    def test_creates_a_private_run_conversation(self, env):
        from db import conversation_store

        autoproject = make_autoproject(env)
        resp = self._post(env, autoproject)
        assert resp.status_code == 201, resp.text
        assert set(resp.json()) == {"conversation_id"}
        cid = resp.json()["conversation_id"]
        meta = _run(conversation_store.get_conversation_meta(uid(env), cid))
        assert meta["autoproject_id"] == autoproject["id"]
        assert meta["origin"] == "autoproject"
        assert meta["project_id"] is None
        assert (env.dirs["chats"] / cid).is_dir()
        # No run row until the message arrives.
        assert _run(env.store.get_run_by_conversation(cid)) is None
        assert _run(env.store.count_runs(autoproject["id"])) == 0

    def test_active_run_409(self, env):
        autoproject = make_autoproject(env)
        run = start_run(env, autoproject)
        resp = self._post(env, autoproject)
        assert resp.status_code == 409
        body = detail(resp)
        assert body["error"] == "autoproject_run_active"
        assert body["active_run"] == {"id": run["id"], "seq": 1, "status": "running"}

    def test_waiting_run_is_active_too(self, env):
        autoproject = make_autoproject(env)
        set_waiting(env, start_run(env, autoproject))
        assert detail(self._post(env, autoproject))["error"] == "autoproject_run_active"

    def test_archived_409(self, env):
        autoproject = make_autoproject(env)
        client(env).put(f"/app/api/autoprojects/{autoproject['id']}/archive")
        resp = self._post(env, autoproject)
        assert resp.status_code == 409
        assert detail(resp)["error"] == "autoproject_archived"

    def test_earlier_unsent_conversations_are_dropped(self, env, monkeypatch):
        """Repeated starts that never send cannot pile up conversations:
        creating one drops this AutoProject's unsent ones past the grace
        period, and nothing else."""
        from db import conversation_store

        autoproject = make_autoproject(env)
        other = make_autoproject(env, title="Other")
        sent = finished_run(env, autoproject)
        first = self._post(env, autoproject).json()["conversation_id"]
        other_unsent = self._post(env, other).json()["conversation_id"]

        # Within the grace period the earlier start survives.
        second = self._post(env, autoproject).json()["conversation_id"]
        assert _run(conversation_store.get_conversation_meta(uid(env), first)) is not None

        monkeypatch.setattr(service, "AUTOPROJECT_UNSENT_RUN_CONVERSATION_GRACE", timedelta(0))
        third = self._post(env, autoproject).json()["conversation_id"]
        for gone in (first, second):
            assert _run(conversation_store.get_conversation_meta(uid(env), gone)) is None
            assert not (env.dirs["chats"] / gone).exists()
        for kept in (third, other_unsent, sent["conversation_id"]):
            assert _run(conversation_store.get_conversation_meta(uid(env), kept)) is not None


class TestDeleteRun:
    def _delete(self, env, autoproject, run_id, who="alice"):
        return client(env, who).delete(f"/app/api/autoprojects/{autoproject['id']}/runs/{run_id}")

    def test_deletes_entry_and_conversation(self, env):
        from db import conversation_store

        autoproject = make_autoproject(env)
        keep = finished_run(env, autoproject)
        run = finished_run(env, autoproject)
        cid = run["conversation_id"]
        (env.dirs["chats"] / cid / "workspace").mkdir(parents=True, exist_ok=True)
        resp = self._delete(env, autoproject, run["id"])
        assert resp.status_code == 200 and resp.json() == {"success": True}
        assert _run(env.store.get_run(run["id"])) is None
        assert _run(conversation_store.get_conversation_meta(uid(env), cid)) is None
        assert not (env.dirs["chats"] / cid).exists()
        assert _run(env.store.get_run(keep["id"])) is not None
        assert {"autoproject_changed", "autoproject_list_changed"} <= set(event_types(env))
        # seq is never reused.
        assert start_run(env, autoproject)["seq"] == 3

    @pytest.mark.parametrize("status", ["running", "waiting"])
    def test_active_run_409(self, env, status):
        autoproject = make_autoproject(env)
        run = start_run(env, autoproject)
        if status == "waiting":
            set_waiting(env, run)
        resp = self._delete(env, autoproject, run["id"])
        assert resp.status_code == 409
        assert detail(resp)["error"] == "autoproject_run_active"
        assert _run(env.store.get_run(run["id"]))["status"] == status

    def test_run_of_another_autoproject_or_missing_404(self, env):
        mine = make_autoproject(env)
        other = make_autoproject(env, title="Other")
        other_run = finished_run(env, other)
        for run_id in (other_run["id"], str(uuid.uuid4())):
            resp = self._delete(env, mine, run_id)
            assert resp.status_code == 404
            assert detail(resp)["error"] == "autoproject_run_not_found"
        assert _run(env.store.get_run(other_run["id"])) is not None


class TestAnswerQuestion:
    def _answer(self, env, autoproject, handle_id, answer, who="alice"):
        return client(env, who).post(
            f"/app/api/autoprojects/{autoproject['id']}/questions/{handle_id}/answer",
            json={"answer": answer},
        )

    def _waiting(self, env, autoproject=None):
        autoproject = autoproject or make_autoproject(env)
        run = start_run(env, autoproject)
        set_waiting(env, run)
        handle = ask(env, run)
        env.published.clear()
        return autoproject, run, handle

    def test_answer_resolves_rewrites_and_kicks_resume(self, env):
        autoproject, run, handle = self._waiting(env)
        resp = self._answer(env, autoproject, handle["id"], "  Red  ")
        assert resp.status_code == 200, resp.text
        view = resp.json()
        assert (view["handle_id"], view["status"], view["answer"]) == (handle["id"], "answered", "Red")
        assert env.kicked == [(uid(env), run["conversation_id"])]
        [message] = [m for m in read_transcript(run["conversation_id"])["messages"]
                     if m.get("type") == "autoproject_question"]
        assert (message["status"], message["answer"]) == ("answered", "Red")
        types = event_types(env)
        assert "wait_handle_resolved" in types and "autoproject_changed" in types
        # The run is still waiting until its resume starts: the list row did
        # not change.
        assert "autoproject_list_changed" not in types

    def test_second_answer_409_question_resolved(self, env):
        autoproject, _run_row, handle = self._waiting(env)
        self._answer(env, autoproject, handle["id"], "Red")
        env.kicked.clear()
        resp = self._answer(env, autoproject, handle["id"], "Blue")
        assert resp.status_code == 409
        assert detail(resp)["error"] == "question_resolved"
        assert (detail(resp)["status"], detail(resp)["answer"]) == ("answered", "Red")
        assert env.kicked == []

    def test_pending_question_of_a_finished_run_409(self, env):
        autoproject, run, handle = self._waiting(env)
        _run(env.store.set_run_status(run["id"], "stopped"))
        resp = self._answer(env, autoproject, handle["id"], "Red")
        assert resp.status_code == 409
        assert (detail(resp)["error"], detail(resp)["status"]) == ("question_resolved", "cancelled")
        assert env.kicked == []

    @pytest.mark.parametrize("answer", [
        "", "   ", None, 5, ["Red"], "a" * (AUTOPROJECT_ANSWER_MAX_CHARS + 1),
    ], ids=["empty", "blank", "null", "int", "list", "too_long"])
    def test_invalid_answer_400(self, env, answer):
        autoproject, run, handle = self._waiting(env)
        resp = self._answer(env, autoproject, handle["id"], answer)
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_answer"
        assert env.kicked == []
        from db import tool_wait_handle_store

        assert _run(tool_wait_handle_store.get_handle(handle["id"]))["status"] == "pending"

    def test_missing_foreign_and_other_autoproject_questions_read_the_same(self, env):
        autoproject, _run_row, _handle = self._waiting(env)
        _other, _other_run, other_handle = self._waiting(env, make_autoproject(env, title="B"))
        bob_ap = make_autoproject(env, "bob", title="Bob")
        bob_run = start_run(env, bob_ap)
        set_waiting(env, bob_run)
        bob_handle = ask(env, bob_run, who="bob")
        # An approval card's handle is not a question either.
        card_run_ap = make_autoproject(env, title="C")
        card_run = start_run(env, card_run_ap)
        _req, card_handle, _row = open_action_card(env, card_run)

        bodies = []
        for handle_id in (other_handle["id"], bob_handle["id"], str(uuid.uuid4()),
                          card_handle["id"]):
            resp = self._answer(env, autoproject, handle_id, "Red")
            assert resp.status_code == 404
            assert detail(resp)["error"] == "question_not_found"
            bodies.append(resp.content.replace(handle_id.encode(), b"H"))
        assert len(set(bodies)) == 1
        assert env.kicked == []

    def test_generic_wait_handle_route_refuses_questions(self, env):
        """A question has its own answer route (validation, gate, transcript
        rewrite); the generic resolve route must not resolve one."""
        from chat.wait_handle_routes import router as wait_handle_router
        from db import tool_wait_handle_store

        _autoproject, _run_row, handle = self._waiting(env)
        resp = client(env, "alice", wait_handle_router).post(
            f"/app/api/wait-handles/{handle['id']}/resolve",
            json={"status": "accepted", "response": {"answer": "sneaky"}},
        )
        assert resp.status_code == 409, resp.text
        assert detail(resp)["error"] == "use_autoproject_answer_route"
        assert _run(tool_wait_handle_store.get_handle(handle["id"]))["status"] == "pending"
        assert env.kicked == []


# ---------------------------------------------------------------------------
# Read, archive, delete
# ---------------------------------------------------------------------------


class TestRead:
    def _unread(self, env):
        autoproject = make_autoproject(env)
        run = finished_run(env, autoproject)
        _run(env.store.mark_autoproject_unread(autoproject["id"]))
        return _run(env.store.get_autoproject(uid(env), autoproject["id"])), run

    def test_ack_moves_read_seq_and_publishes_list(self, env):
        autoproject, _run_row = self._unread(env)
        assert autoproject["unread"] is True
        env.published.clear()
        resp = client(env).post(
            f"/app/api/autoprojects/{autoproject['id']}/read",
            json={"seq": autoproject["event_seq"]},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "event_seq": autoproject["event_seq"], "unread_seq": autoproject["unread_seq"],
            "read_seq": autoproject["event_seq"], "unread": False,
        }
        assert event_types(env) == ["autoproject_list_changed"]

    def test_ack_is_clamped_and_never_moves_back(self, env):
        autoproject, _run_row = self._unread(env)
        c = client(env)
        url = f"/app/api/autoprojects/{autoproject['id']}/read"
        assert c.post(url, json={"seq": 10_000}).json()["read_seq"] == autoproject["event_seq"]
        env.published.clear()
        assert c.post(url, json={"seq": 0}).json()["read_seq"] == autoproject["event_seq"]
        assert env.published == []

    def test_impersonating_admin_changes_nothing(self, env):
        autoproject, _run_row = self._unread(env)
        admin_view = {**env.users["alice"], "_impersonator_uid": 999}
        resp = client(env, admin_view).post(
            f"/app/api/autoprojects/{autoproject['id']}/read",
            json={"seq": autoproject["event_seq"]},
        )
        assert resp.status_code == 200 and resp.json()["unread"] is True
        assert _run(env.store.get_autoproject(uid(env), autoproject["id"]))["unread"] is True

    @pytest.mark.parametrize("body", [{}, {"seq": -1}, {"seq": _MAX_INT + 1}, {"seq": "x"}])
    def test_bad_seq_422(self, env, body):
        autoproject = make_autoproject(env)
        resp = client(env).post(f"/app/api/autoprojects/{autoproject['id']}/read", json=body)
        assert resp.status_code == 422


class TestArchive:
    def test_archive_and_unarchive(self, env):
        autoproject = make_autoproject(env)
        finished_run(env, autoproject)
        _run(env.store.mark_autoproject_unread(autoproject["id"]))
        c = client(env)
        resp = c.put(f"/app/api/autoprojects/{autoproject['id']}/archive")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data) == DETAIL_KEYS
        # Archiving marks it read: an archived AutoProject has no unread dot.
        assert data["archived"] is True and data["unread"] is False
        assert event_types(env) == ["autoproject_changed", "autoproject_list_changed"]
        # Idempotent.
        assert c.put(f"/app/api/autoprojects/{autoproject['id']}/archive").status_code == 200
        # Still readable while archived.
        assert c.get(f"/app/api/autoprojects/{autoproject['id']}/runs").status_code == 200
        resp = c.put(f"/app/api/autoprojects/{autoproject['id']}/unarchive")
        assert resp.status_code == 200 and resp.json()["archived"] is False

    @pytest.mark.parametrize("status", ["running", "waiting"])
    def test_archive_refused_while_a_run_is_active(self, env, status):
        autoproject = make_autoproject(env)
        run = start_run(env, autoproject)
        if status == "waiting":
            set_waiting(env, run)
        resp = client(env).put(f"/app/api/autoprojects/{autoproject['id']}/archive")
        assert resp.status_code == 409
        assert detail(resp)["error"] == "autoproject_run_active"
        assert detail(resp)["active_run"]["status"] == status
        assert _run(env.store.get_autoproject(uid(env), autoproject["id"]))["archived"] is False
        assert env.published == []

    def test_unarchive_counts_against_the_cap(self, env, monkeypatch):
        archived = make_autoproject(env, title="Old")
        c = client(env)
        c.put(f"/app/api/autoprojects/{archived['id']}/archive")
        monkeypatch.setattr(env.store, "MAX_ACTIVE_AUTOPROJECTS_PER_USER", 1)
        make_autoproject(env, title="New")
        resp = c.put(f"/app/api/autoprojects/{archived['id']}/unarchive")
        assert resp.status_code == 409
        assert detail(resp)["error"] == "autoprojects_cap_reached"
        assert _run(env.store.get_autoproject(uid(env), archived["id"]))["archived"] is True


class TestDeleteAutoProject:
    def test_deletes_rows_conversations_and_directories(self, env):
        from chat.storage import ChatStorage
        from db import conversation_store

        autoproject = make_autoproject(env)
        keep = make_autoproject(env, title="Keep")
        keep_run = finished_run(env, keep)
        runs = [finished_run(env, autoproject) for _ in range(2)]
        unsent = client(env).post(
            f"/app/api/autoprojects/{autoproject['id']}/runs",
        ).json()["conversation_id"]
        workspace = ChatStorage.get_autoproject_workspace_root(autoproject["id"])
        workspace.mkdir(parents=True)
        (workspace / "notes.md").write_text("x")
        env.published.clear()

        resp = client(env).delete(f"/app/api/autoprojects/{autoproject['id']}")
        assert resp.status_code == 200 and resp.json() == {"success": True}

        assert _run(env.store.get_autoproject(uid(env), autoproject["id"])) is None
        for cid in [r["conversation_id"] for r in runs] + [unsent]:
            assert _run(conversation_store.get_conversation_meta(uid(env), cid)) is None
            assert not (env.dirs["chats"] / cid).exists()
            assert _run(env.store.get_run_by_conversation(cid)) is None
        assert not (env.dirs["autoprojects"] / autoproject["id"]).exists()
        assert _run(env.store.list_brief_versions(autoproject["id"])) == []
        # The other AutoProject is untouched.
        assert _run(env.store.get_run(keep_run["id"])) is not None
        assert (env.dirs["chats"] / keep_run["conversation_id"]).exists()
        assert event_types(env)[-2:] == ["autoproject_changed", "autoproject_list_changed"]
        assert client(env).get(f"/app/api/autoprojects/{autoproject['id']}").status_code == 404

    def test_active_run_is_stopped_and_its_cards_closed(self, env):
        from db import action_request_store, tool_wait_handle_store

        autoproject = make_autoproject(env)
        run = start_run(env, autoproject)
        set_waiting(env, run)
        question = ask(env, run)
        request, card_handle, _row = open_action_card(env, run)

        resp = client(env).delete(f"/app/api/autoprojects/{autoproject['id']}")
        assert resp.status_code == 200, resp.text
        # The card cannot be approved later with no run behind it.
        after = _run(action_request_store.get_action_request(uid(env), request["id"]))
        assert after["status"] == "stopped"
        for handle in (question, card_handle):
            row = _run(tool_wait_handle_store.get_handle(handle["id"]))
            assert row is None or row["status"] != "pending"
        assert "request_count_changed" in event_types(env)


# ---------------------------------------------------------------------------
# Oversized integers (422, never a 500)
# ---------------------------------------------------------------------------


class TestOversizedIntegers:
    CASES = [
        ("GET", "/app/api/autoprojects/{id}/brief/versions/{big}", None),
        ("POST", "/app/api/autoprojects/{id}/brief/versions/{big}/restore",
         {"expected_brief_version": 0}),
        ("POST", "/app/api/autoprojects/{id}/brief/versions/1/restore",
         {"expected_brief_version": "BIG"}),
        ("PUT", "/app/api/autoprojects/{id}/brief", {"brief": "x", "expected_brief_version": "BIG"}),
        ("GET", "/app/api/autoprojects/{id}/runs?before_seq={big}", None),
        ("POST", "/app/api/autoprojects/{id}/read", {"seq": "BIG"}),
    ]

    @pytest.mark.parametrize("big", [_MAX_INT + 1, 2**63, 10**30], ids=["2^31", "2^63", "1e30"])
    @pytest.mark.parametrize("method,url,payload", CASES, ids=[f"{m} {u}" for m, u, _ in CASES])
    def test_422(self, env, method, url, payload, big):
        autoproject = make_autoproject(env)
        if payload is not None:
            payload = {k: (big if v == "BIG" else v) for k, v in payload.items()}
        resp = _request(client(env), method, url.format(id=autoproject["id"], big=big), payload)
        assert resp.status_code == 422, resp.text

    def test_max_int_itself_is_accepted(self, env):
        autoproject = make_autoproject(env)
        c = client(env)
        base = f"/app/api/autoprojects/{autoproject['id']}"
        assert c.get(f"{base}/brief/versions/{_MAX_INT}").status_code == 404
        assert c.get(f"{base}/runs", params={"before_seq": _MAX_INT}).status_code == 200
        assert c.post(f"{base}/read", json={"seq": _MAX_INT}).status_code == 200
        resp = c.put(f"{base}/brief", json={"brief": "x", "expected_brief_version": _MAX_INT})
        assert resp.status_code == 409 and detail(resp)["error"] == "stale_brief"
