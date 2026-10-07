"""Project doc sources: the per-project "Docs access" setting that lets a
private project's conversations read the Quest Docs of chosen public
projects.

Covered here, on the isolated ``docs_env`` of tests/test_docs_service.py:

1. Store: ``set_doc_source_projects`` validates ownership, modes and
   self-reference, replaces the list, collapses duplicates, leaves
   ``updated_at`` alone; ``list_doc_source_projects`` returns the rows by
   name; links cascade with either project.
2. Routes: GET / PUT ``/app/api/projects/{id}/doc-sources`` incl. the 404s
   for unknown and other users' projects and the 400 codes.

The read side (what the conversations then see) is in
tests/test_docs_service.py ``TestDocSources`` and tests/test_docs_access.py.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_docs_service import (  # noqa: F401  (fixture + helpers)
    _run,
    docs_env,
    give_doc_source,
)


def client(env, who="alice"):
    from chat import project_routes

    app = FastAPI()
    app.include_router(project_routes.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[
        project_routes.get_current_user_cookie_or_apikey_checked
    ] = _current
    return TestClient(app)


def detail(resp):
    return resp.json()["detail"]


def store():
    import db.project_store as project_store
    return project_store


def _row(project):
    return {
        "id": project["id"], "name": project["name"],
        "public": project["public"], "archived": project["archived"],
    }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TestStore:
    def test_replace_list_and_order_by_name(self, docs_env):
        alice = docs_env.users["alice"]["id"]
        zed = _run(store().create_project(alice, "Zed", public=True))
        assert _run(store().list_doc_source_project_ids(docs_env.private_project)) == []
        assert _run(store().list_doc_source_projects(alice, docs_env.private_project)) == []

        rows = give_doc_source(docs_env, docs_env.private_project, zed["id"], docs_env.public_project)
        assert [r["name"] for r in rows] == ["Open", "Zed"]
        assert sorted(_run(store().list_doc_source_project_ids(docs_env.private_project))) == sorted(
            [zed["id"], docs_env.public_project]
        )
        assert [r["id"] for r in _run(store().list_doc_source_projects(alice, docs_env.private_project))] == [
            docs_env.public_project, zed["id"],
        ]
        # Replacement drops what is no longer listed; duplicates collapse.
        rows = give_doc_source(docs_env, docs_env.private_project, zed["id"], zed["id"])
        assert [r["id"] for r in rows] == [zed["id"]]
        assert _run(store().list_doc_source_project_ids(docs_env.private_project)) == [zed["id"]]
        assert give_doc_source(docs_env, docs_env.private_project) == []
        assert _run(store().list_doc_source_project_ids(docs_env.private_project)) == []

    def test_leaves_updated_at_alone(self, docs_env):
        alice = docs_env.users["alice"]["id"]
        before = _run(store().get_project(alice, docs_env.private_project))["updated_at"]
        give_doc_source(docs_env, docs_env.private_project, docs_env.public_project)
        assert _run(store().get_project(alice, docs_env.private_project))["updated_at"] == before

    def test_unknown_or_foreign_project_is_none(self, docs_env):
        bob = docs_env.users["bob"]["id"]
        assert _run(store().set_doc_source_projects(bob, docs_env.private_project, [])) is None
        assert _run(store().set_doc_source_projects(
            docs_env.users["alice"]["id"], str(uuid.uuid4()), [],
        )) is None

    @pytest.mark.parametrize("bad", ["", "   ", 7, None])
    def test_rejects_malformed_ids(self, docs_env, bad):
        with pytest.raises(store().ProjectDocSourceError) as exc:
            _run(store().set_doc_source_projects(
                docs_env.users["alice"]["id"], docs_env.private_project, [bad],
            ))
        assert exc.value.code == "invalid_doc_source"

    def test_refuses_public_target_private_source_self_and_strangers(self, docs_env):
        alice = docs_env.users["alice"]["id"]
        bob = docs_env.users["bob"]["id"]
        bobs_public = _run(store().create_project(bob, "Bob open", public=True))

        with pytest.raises(store().ProjectDocSourceError) as exc:
            give_doc_source(docs_env, docs_env.public_project, docs_env.public_project)
        assert exc.value.code == "public_project_no_doc_sources"

        for source in (docs_env.other_project, docs_env.private_project, bobs_public["id"], str(uuid.uuid4())):
            with pytest.raises(store().ProjectDocSourceError) as exc:
                give_doc_source(docs_env, docs_env.private_project, docs_env.public_project, source)
            assert exc.value.code == "invalid_doc_source"
            # Nothing changed (the whole list is refused).
            assert _run(store().list_doc_source_project_ids(docs_env.private_project)) == []
        assert _run(store().list_doc_source_project_ids(docs_env.private_project)) == []
        assert _run(store().get_project(alice, docs_env.private_project))["public"] is False

    def test_links_cascade_with_either_project(self, docs_env):
        alice = docs_env.users["alice"]["id"]
        other_private = _run(store().create_project(alice, "Other private"))["id"]
        give_doc_source(docs_env, docs_env.private_project, docs_env.public_project)
        give_doc_source(docs_env, other_private, docs_env.public_project)
        assert _run(store().delete_project(alice, other_private)) is True
        assert _run(store().list_doc_source_project_ids(docs_env.private_project)) == [
            docs_env.public_project,
        ]
        assert _run(store().delete_project(alice, docs_env.public_project)) is True
        assert _run(store().list_doc_source_project_ids(docs_env.private_project)) == []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class TestRoutes:
    def test_get_and_put_round_trip(self, docs_env):
        alice = docs_env.users["alice"]["id"]
        public = _run(store().get_project(alice, docs_env.public_project))
        c = client(docs_env)
        url = f"/app/api/projects/{docs_env.private_project}/doc-sources"

        assert c.get(url).json() == {"sources": []}
        resp = c.put(url, json={"source_project_ids": [docs_env.public_project]})
        assert resp.status_code == 200
        assert resp.json() == {"sources": [_row(public)]}
        assert c.get(url).json() == {"sources": [_row(public)]}
        assert _run(store().list_doc_source_project_ids(docs_env.private_project)) == [
            docs_env.public_project,
        ]
        resp = c.put(url, json={"source_project_ids": []})
        assert resp.status_code == 200 and resp.json() == {"sources": []}
        assert _run(store().list_doc_source_project_ids(docs_env.private_project)) == []

    def test_archived_source_is_listed_with_its_flag(self, docs_env):
        alice = docs_env.users["alice"]["id"]
        give_doc_source(docs_env, docs_env.private_project, docs_env.public_project)
        _run(store().set_project_archived(alice, docs_env.public_project, True))
        rows = client(docs_env).get(
            f"/app/api/projects/{docs_env.private_project}/doc-sources"
        ).json()["sources"]
        assert [(r["id"], r["archived"]) for r in rows] == [(docs_env.public_project, True)]

    def test_404_for_unknown_and_foreign_projects(self, docs_env):
        for who, project_id in (
            ("alice", str(uuid.uuid4())),
            ("bob", docs_env.private_project),
        ):
            c = client(docs_env, who)
            for method, payload in (("get", None), ("put", {"source_project_ids": []})):
                kwargs = {"json": payload} if payload is not None else {}
                resp = getattr(c, method)(f"/app/api/projects/{project_id}/doc-sources", **kwargs)
                assert resp.status_code == 404, (who, method)
                assert detail(resp)["error"] == "not_found"

    def test_400_codes(self, docs_env):
        c = client(docs_env)
        resp = c.put(
            f"/app/api/projects/{docs_env.public_project}/doc-sources",
            json={"source_project_ids": [docs_env.public_project]},
        )
        assert resp.status_code == 400
        assert detail(resp)["error"] == "public_project_no_doc_sources"
        for source in (docs_env.other_project, docs_env.private_project, str(uuid.uuid4())):
            resp = c.put(
                f"/app/api/projects/{docs_env.private_project}/doc-sources",
                json={"source_project_ids": [source]},
            )
            assert resp.status_code == 400, source
            assert detail(resp)["error"] == "invalid_doc_source"
        resp = c.put(
            f"/app/api/projects/{docs_env.private_project}/doc-sources",
            json={"source_project_ids": "not-a-list"},
        )
        assert resp.status_code == 422
