"""Quest Docs human editing: chat/docs/ui_writes.py, chat/docs/edit_routes.py
and ``files.delete_asset``.

Runs the base docs router plus the editing router in a bare FastAPI app with
the auth dependency overridden (the tests/test_docs_routes.py pattern), on
the isolated ``docs_env`` from tests/test_docs_service.py (tmp SQLite, tmp
data dirs, docs gate open, realtime events captured). Users: alice (owns
the docs and projects), bob, carol.

Covers:

1. ``PUT /docs/{id}/content``: owner and write-share saves (no approval
   card), read share 403, stranger 404 (same body as a missing id), token
   required / stale token -> flat 409, identical body -> no write, CRLF
   normalization, size cap, non-string / lone-surrogate content, the
   revision snapshot (sidecar = the PREVIOUS writer) and ``doc.meta.json`` /
   ``last_write_source`` = ``ui:<user_id>``, events, concurrent saves with
   one token (exactly one wins), a share revoked while a save queued.
2. ``POST /docs/{id}/assets``: the 201 shape, name sanitizing + ``-2``
   suffix, alt default and escaping, capped read, ``invalid_image`` (SVG,
   renamed text), ``image_too_large``, ``asset_limit``, access, malformed
   forms; asset-only write (no body change, no snapshot).
3. ``DELETE /docs/{id}/assets/{name}``: owner only, traversal / hidden /
   symlinked names 404 ``asset_not_found`` with nothing outside ``assets/``
   touched, ``asset_in_use`` against the CURRENT body (name-boundary
   regex), asset-only write.
4. Public-project docs: the owner edits from the UI; hidden (404) while the
   ``public_projects`` gate is closed. The ``docs`` gate closes every route.
"""

import asyncio
import json
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.docs import constants
from chat.docs import files
from chat.docs.constants import doc_not_found_message, docs_disabled_message
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    _run,
    body,
    docs_env,
    seed_doc,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 48
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture()
def env(docs_env, monkeypatch):
    """``docs_env`` with the user and action-request stores on the same tmp
    DB (row enrichment looks up users and approved-card owners)."""
    import db.action_request_store as action_request_store
    import db.user_store as user_store

    for mod in (user_store, action_request_store):
        monkeypatch.setattr(
            mod, "AsyncSessionLocal", docs_env.doc_store.AsyncSessionLocal,
        )
    return docs_env


def client(env, who="alice"):
    from chat.docs import edit_routes, routes

    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(edit_routes.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[get_current_user_cookie_or_apikey_checked] = _current
    return TestClient(app)


def ui_writes():
    from chat.docs import ui_writes as mod
    return mod


def uid(env, who):
    return env.users[who]["id"]


def detail(resp):
    return resp.json()["detail"]


def row(env, doc_id):
    return _run(env.doc_store.get_doc(doc_id))


def token(env, doc_id):
    return row(env, doc_id)["updated_at"]


def share(env, doc, who, permission):
    user_id = None if who is None else uid(env, who)
    return _run(env.doc_store.add_share(doc["id"], user_id, permission))


def put_content(c, doc_id, content, expected):
    return c.put(
        f"/app/api/docs/{doc_id}/content",
        json={"content": content, "expected_updated_at": expected},
    )


def upload(c, doc_id, data, filename="chart.png", alt=None, mime="image/png"):
    form = {"alt": alt} if alt is not None else None
    return c.post(
        f"/app/api/docs/{doc_id}/assets",
        files={"file": (filename, data, mime)},
        data=form,
    )


def revisions(doc_id):
    return files.list_revisions(doc_id)


def asset_dir(env, doc_id):
    return env.dirs["docs"] / doc_id / "assets"


def events_for(env, user_id):
    return [ev for who, ev in env.published if who == user_id]


def assert_write_events(env, user_id, doc_id, updated_at):
    evs = events_for(env, user_id)
    assert {"type": "doc_changed", "doc_id": doc_id, "updated_at": updated_at} in evs
    assert {"type": "doc_list_changed"} in evs


def not_found(doc_id):
    return {"error": "doc_not_found", "message": doc_not_found_message(doc_id)}


def spy_request_stream(monkeypatch):
    """Record the size of every body chunk any handler pulls."""
    from starlette.requests import Request

    pulled = []
    real_stream = Request.stream

    async def spy(self):
        async for chunk in real_stream(self):
            pulled.append(len(chunk))
            yield chunk

    monkeypatch.setattr(Request, "stream", spy)
    return pulled


def multipart_chunks(image, chunk_size, *, total, boundary=b"BOUND"):
    """A one-file multipart body (``file`` = ``image`` padded with zeros to
    ``total`` bytes) cut into ``chunk_size`` pieces, lazily."""
    head = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="chart.png"\r\n'
        b"Content-Type: image/png\r\n\r\n"
    )
    tail = b"\r\n--" + boundary + b"--\r\n"

    def payload():
        yield head
        yield image
        remaining = total - len(image)
        while remaining > 0:
            n = min(chunk_size, remaining)
            yield b"\x00" * n
            remaining -= n
        yield tail

    buf = b""
    for piece in payload():
        buf += piece
        while len(buf) >= chunk_size:
            yield buf[:chunk_size]
            buf = buf[chunk_size:]
    if buf:
        yield buf


async def asgi_upload(env, doc_id, chunks, who="alice"):
    """POST an upload straight through ASGI with NO Content-Length (a
    chunked body), counting the bytes the app pulls from ``receive``.

    Returns ``(status, json_body, bytes_pulled)``.
    """
    from chat.docs import edit_routes, routes

    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(edit_routes.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[get_current_user_cookie_or_apikey_checked] = _current
    chunks = iter(chunks)
    pulled = 0
    sent = []

    async def receive():
        nonlocal pulled
        try:
            chunk = next(chunks)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        pulled += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message):
        sent.append(message)

    path = f"/app/api/docs/{doc_id}/assets"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"multipart/form-data; boundary=BOUND"),
            (b"transfer-encoding", b"chunked"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(raw), pulled


async def until_waiting(lock):
    """Wait until some task is blocked on ``lock`` (asyncio.Lock)."""
    for _ in range(500):
        if getattr(lock, "_waiters", None):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no task queued on the lock")


def run_queued(doc_id, make_coro, while_waiting):
    """Hold the doc's write lock, start ``make_coro()``, let it queue on the
    lock (past its pre-lock checks), run ``while_waiting()`` (sync or
    async), release, and return the coroutine's result or exception."""
    from chat.docs import service

    async def scenario():
        lock = service._write_lock(doc_id)
        await lock.acquire()
        try:
            task = asyncio.ensure_future(make_coro())
            await until_waiting(lock)
            outcome = while_waiting()
            if asyncio.iscoroutine(outcome):
                await outcome
        finally:
            lock.release()
        (result,) = await asyncio.gather(task, return_exceptions=True)
        return result

    return _run(scenario())


# ---------------------------------------------------------------------------
# PUT /docs/{id}/content
# ---------------------------------------------------------------------------


class TestReplaceContent:
    def test_owner_save_snapshots_previous_writer(self, env):
        doc = seed_doc(env)
        alice = uid(env, "alice")
        # The current body was written by a conversation.
        files.write_body(doc["id"], "v1\n", write_source="conversation:abc")
        before = token(env, doc["id"])
        n_before = len(revisions(doc["id"]))
        env.published.clear()

        resp = put_content(client(env), doc["id"], "# v2\n\nnew text\n", before)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["changed"] is True
        assert data["content"] == "# v2\n\nnew text\n"
        assert data["id"] == doc["id"]
        assert data["updated_at"] != before
        assert data["content_size"] == len(b"# v2\n\nnew text\n")
        assert data["last_write_source"] == f"ui:{alice}"
        assert data["access"]["write"] == "free"

        assert body(doc["id"]) == "# v2\n\nnew text\n"
        stored = row(env, doc["id"])
        assert stored["updated_at"] == data["updated_at"]
        assert stored["last_write_source"] == f"ui:{alice}"
        assert stored["content_size"] == data["content_size"]

        # The replaced body is the newest revision; its sidecar names the
        # conversation that wrote it, and doc.meta.json the UI writer.
        revs = revisions(doc["id"])
        assert len(revs) == n_before + 1
        assert revs[-1].read_text() == "v1\n"
        assert files.read_revision_meta(revs[-1])["source"] == "conversation:abc"
        meta = files.read_doc_meta(doc["id"])
        assert meta["source"] == f"ui:{alice}"
        assert meta["size"] == data["content_size"]

        assert_write_events(env, alice, doc["id"], data["updated_at"])

    def test_second_save_sidecar_names_the_ui_writer(self, env):
        doc = seed_doc(env)
        alice = uid(env, "alice")
        c = client(env)
        first = put_content(c, doc["id"], "one\n", token(env, doc["id"])).json()
        second = put_content(c, doc["id"], "two\n", first["updated_at"])
        assert second.status_code == 200
        revs = revisions(doc["id"])
        assert revs[-1].read_text() == "one\n"
        assert files.read_revision_meta(revs[-1])["source"] == f"ui:{alice}"

    def test_crlf_normalized(self, env):
        doc = seed_doc(env)
        resp = put_content(client(env), doc["id"], "a\r\nb\r\n", token(env, doc["id"]))
        assert resp.status_code == 200
        assert resp.json()["content"] == "a\nb\n"
        assert resp.json()["content_size"] == 4
        assert body(doc["id"]) == "a\nb\n"

    def test_identical_body_writes_nothing(self, env):
        doc = seed_doc(env, content="same\ntext\n")
        before = row(env, doc["id"])
        n_revs = len(revisions(doc["id"]))
        meta = files.read_doc_meta(doc["id"])
        env.published.clear()

        # Identical after CRLF -> LF too.
        resp = put_content(client(env), doc["id"], "same\r\ntext\r\n", before["updated_at"])
        assert resp.status_code == 200
        data = resp.json()
        assert data["changed"] is False
        assert data["content"] == "same\ntext\n"
        assert data["updated_at"] == before["updated_at"]
        after = row(env, doc["id"])
        assert after["updated_at"] == before["updated_at"]
        assert after["last_write_source"] == before["last_write_source"]
        assert len(revisions(doc["id"])) == n_revs
        assert files.read_doc_meta(doc["id"]) == meta
        assert env.published == []

    @pytest.mark.parametrize("payload", [
        {"content": "x"},
        {"content": "x", "expected_updated_at": ""},
        {"content": "x", "expected_updated_at": 12},
        {"expected_updated_at": "TOKEN"},
        {"content": 42, "expected_updated_at": "TOKEN"},
        {"content": ["x"], "expected_updated_at": "TOKEN"},
        {"content": None, "expected_updated_at": "TOKEN"},
        {},
    ])
    def test_missing_or_bad_fields_400(self, env, payload):
        doc = seed_doc(env)
        payload = {
            k: (token(env, doc["id"]) if v == "TOKEN" else v) for k, v in payload.items()
        }
        resp = client(env).put(f"/app/api/docs/{doc['id']}/content", json=payload)
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_request"
        assert body(doc["id"]) == "line one\nline two\n"

    def test_no_body_400(self, env):
        doc = seed_doc(env)
        resp = client(env).put(f"/app/api/docs/{doc['id']}/content")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_request"

    @pytest.mark.parametrize("raw,content_type", [
        (b'["content", "x"]', "application/json"),
        (b'"just a string"', "application/json"),
        (b"42", "application/json"),
        (b"null", "application/json"),
        (b'{"content": "x", ', "application/json"),
        (b"\xff\xfe\x00garbage", "application/json"),
        (b'{"content": "x", "expected_updated_at": "t"}', "text/plain"),
        (b"content=x&expected_updated_at=t", "application/x-www-form-urlencoded"),
    ])
    def test_non_object_or_non_json_body_400_not_422(self, env, raw, content_type):
        doc = seed_doc(env)
        resp = client(env).put(
            f"/app/api/docs/{doc['id']}/content",
            content=raw,
            headers={"Content-Type": content_type},
        )
        assert resp.status_code == 400, resp.text
        assert detail(resp)["error"] == "invalid_request"
        assert body(doc["id"]) == "line one\nline two\n"

    def test_json_object_without_content_type_accepted(self, env):
        doc = seed_doc(env)
        raw = json.dumps({"content": "ok\n", "expected_updated_at": token(env, doc["id"])})
        resp = client(env).put(f"/app/api/docs/{doc['id']}/content", content=raw.encode())
        assert resp.status_code == 200, resp.text
        assert body(doc["id"]) == "ok\n"

    def test_declared_body_over_limit_refused_unread(self, env, monkeypatch):
        doc = seed_doc(env)
        expected = token(env, doc["id"])
        monkeypatch.setattr(constants, "DOC_MAX_CONTENT_SIZE", 10)
        pulled = spy_request_stream(monkeypatch)
        resp = put_content(client(env), doc["id"], "x" * (70 * 1024), expected)
        assert resp.status_code == 400
        assert detail(resp)["error"] == "content_too_large"
        assert pulled == []

    def test_lone_surrogate_400(self, env):
        doc = seed_doc(env)
        raw = (
            '{"content": "bad \\ud800 text", "expected_updated_at": '
            + json.dumps(token(env, doc["id"])) + "}"
        ).encode("ascii")
        resp = client(env).put(
            f"/app/api/docs/{doc['id']}/content",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_request"
        assert body(doc["id"]) == "line one\nline two\n"

    def test_size_cap_after_crlf(self, env, monkeypatch):
        monkeypatch.setattr(constants, "DOC_MAX_CONTENT_SIZE", 10)
        doc = seed_doc(env, content="")
        c = client(env)
        resp = put_content(c, doc["id"], "x" * 11, token(env, doc["id"]))
        assert resp.status_code == 400
        assert detail(resp)["error"] == "content_too_large"
        assert body(doc["id"]) == ""
        # 11 bytes as sent, 10 after CRLF -> LF: accepted.
        resp = put_content(c, doc["id"], "x" * 8 + "\r\n", token(env, doc["id"]))
        assert resp.status_code == 200
        assert body(doc["id"]) == "x" * 8 + "\n"

    def test_stale_token_flat_409(self, env):
        doc = seed_doc(env)
        c = client(env)
        old = token(env, doc["id"])
        assert put_content(c, doc["id"], "first\n", old).status_code == 200
        current = token(env, doc["id"])
        env.published.clear()

        resp = put_content(c, doc["id"], "second\n", old)
        assert resp.status_code == 409
        data = resp.json()
        assert "detail" not in data
        assert data["error"] == "stale_update"
        assert isinstance(data["message"], str) and data["message"]
        assert data["current"]["id"] == doc["id"]
        assert data["current"]["updated_at"] == current
        assert body(doc["id"]) == "first\n"
        assert env.published == []

    def test_write_share_recipient_saves_without_approval(self, env):
        doc = seed_doc(env)
        share(env, doc, "bob", "write")
        bob = uid(env, "bob")
        env.published.clear()

        resp = put_content(client(env, "bob"), doc["id"], "bob was here\n", token(env, doc["id"]))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["changed"] is True
        assert data["access"]["write"] == "free"
        assert body(doc["id"]) == "bob was here\n"
        assert row(env, doc["id"])["last_write_source"] == f"ui:{bob}"
        assert files.read_doc_meta(doc["id"])["source"] == f"ui:{bob}"
        # The owner hears about it.
        assert_write_events(env, uid(env, "alice"), doc["id"], data["updated_at"])

    def test_everyone_write_share_can_save(self, env):
        doc = seed_doc(env)
        share(env, doc, None, "write")
        resp = put_content(client(env, "carol"), doc["id"], "c\n", token(env, doc["id"]))
        assert resp.status_code == 200
        assert body(doc["id"]) == "c\n"

    def test_owner_of_shared_doc_saves_without_approval(self, env):
        doc = seed_doc(env)
        share(env, doc, "bob", "read")
        resp = put_content(client(env), doc["id"], "owner\n", token(env, doc["id"]))
        assert resp.status_code == 200
        assert body(doc["id"]) == "owner\n"

    def test_read_share_recipient_403(self, env):
        doc = seed_doc(env)
        share(env, doc, "bob", "read")
        resp = put_content(client(env, "bob"), doc["id"], "nope\n", token(env, doc["id"]))
        assert resp.status_code == 403
        assert detail(resp)["error"] == "forbidden"
        assert body(doc["id"]) == "line one\nline two\n"

    def test_stranger_404_same_as_missing(self, env):
        doc = seed_doc(env)
        c = client(env, "carol")
        hidden = put_content(c, doc["id"], "x\n", token(env, doc["id"]))
        missing_id = str(uuid.uuid4())
        missing = put_content(c, missing_id, "x\n", "2026-01-01T00:00:00")
        assert hidden.status_code == missing.status_code == 404
        assert detail(hidden) == not_found(doc["id"])
        assert detail(missing) == not_found(missing_id)
        # Even without a token: visibility is checked first.
        no_token = c.put(f"/app/api/docs/{doc['id']}/content", json={"content": "x"})
        assert no_token.status_code == 404
        assert body(doc["id"]) == "line one\nline two\n"

    def test_project_doc_owner_edit(self, env):
        doc = seed_doc(env, project_id=env.private_project)
        resp = put_content(client(env), doc["id"], "p\n", token(env, doc["id"]))
        assert resp.status_code == 200
        assert body(doc["id"]) == "p\n"


class TestReplaceContentService:
    def test_concurrent_saves_same_token_one_wins(self, env):
        doc = seed_doc(env)
        alice = env.users["alice"]
        start = token(env, doc["id"])
        n_revs = len(revisions(doc["id"]))
        mod = ui_writes()

        async def both():
            return await asyncio.gather(
                mod.replace_body_from_ui(alice, doc["id"], "A\n", expected_updated_at=start),
                mod.replace_body_from_ui(alice, doc["id"], "B\n", expected_updated_at=start),
                return_exceptions=True,
            )

        results = _run(both())
        wins = [r for r in results if isinstance(r, tuple)]
        stale = [r for r in results if isinstance(r, env.doc_store.StaleDocError)]
        assert len(wins) == 1 and len(stale) == 1, results
        updated, written, changed = wins[0]
        assert changed is True
        assert body(doc["id"]) == written
        assert stale[0].current["updated_at"] == updated["updated_at"]
        assert len(revisions(doc["id"])) == n_revs + 1

    def test_share_revoked_while_queued(self, env):
        """The write verdict is re-checked under the per-doc lock: a revoked
        share hides the doc (the one not-found text)."""
        from chat.docs import service

        doc = seed_doc(env)
        grant = share(env, doc, "bob", "write")
        start = token(env, doc["id"])
        result = run_queued(
            doc["id"],
            lambda: ui_writes().replace_body_from_ui(
                env.users["bob"], doc["id"], "late\n", expected_updated_at=start,
            ),
            lambda: env.doc_store.remove_share(doc["id"], grant["id"]),
        )
        assert type(result) is service.DocError
        assert str(result) == doc_not_found_message(doc["id"])
        assert body(doc["id"]) == "line one\nline two\n"

    def test_restore_style_write_source(self, env):
        doc = seed_doc(env)
        updated, _body, changed = _run(ui_writes().replace_body_from_ui(
            env.users["alice"], doc["id"], "restored\n",
            expected_updated_at=token(env, doc["id"]), write_source="ui:custom",
        ))
        assert changed and updated["last_write_source"] == "ui:custom"
        assert files.read_doc_meta(doc["id"])["source"] == "ui:custom"

    def test_ui_write_source(self, env):
        assert ui_writes().ui_write_source({"id": 7}) == "ui:7"


class TestQueuedRaces:
    """What changes while a UI write waits for the per-doc write lock."""

    def test_write_share_downgraded_while_queued_content(self, env):
        from chat.docs import service

        doc = seed_doc(env)
        share(env, doc, "bob", "write")
        start = token(env, doc["id"])
        result = run_queued(
            doc["id"],
            lambda: ui_writes().replace_body_from_ui(
                env.users["bob"], doc["id"], "late\n", expected_updated_at=start,
            ),
            lambda: env.doc_store.add_share(doc["id"], uid(env, "bob"), "read"),
        )
        assert isinstance(result, service.DocRequestError)
        assert result.code == "forbidden"
        assert body(doc["id"]) == "line one\nline two\n"

    def test_write_share_downgraded_while_queued_upload(self, env):
        from chat.docs import service

        doc = seed_doc(env)
        share(env, doc, "bob", "write")
        before = token(env, doc["id"])
        result = run_queued(
            doc["id"],
            lambda: ui_writes().add_asset_from_ui(
                env.users["bob"], doc["id"], "x.png", PNG,
            ),
            lambda: env.doc_store.add_share(doc["id"], uid(env, "bob"), "read"),
        )
        assert isinstance(result, service.DocRequestError)
        assert result.code == "forbidden"
        assert files.list_assets(doc["id"]) == []
        assert token(env, doc["id"]) == before

    def test_public_projects_gate_closing_while_queued(self, env):
        from chat.docs import service

        fg = env.fg
        fg.set_feature_enabled(fg.FEATURE_PUBLIC_PROJECTS, True)
        doc = seed_doc(env, "Open", mode="public", project_id=env.public_project)
        start = token(env, doc["id"])
        result = run_queued(
            doc["id"],
            lambda: ui_writes().replace_body_from_ui(
                env.users["alice"], doc["id"], "late\n", expected_updated_at=start,
            ),
            lambda: fg.set_feature_enabled(fg.FEATURE_PUBLIC_PROJECTS, False),
        )
        assert type(result) is service.DocError
        assert str(result) == doc_not_found_message(doc["id"])
        assert body(doc["id"]) == "line one\nline two\n"

    def test_previous_updated_at_is_read_under_the_lock(self, env):
        doc = seed_doc(env)
        queued_with = token(env, doc["id"])
        landed = {}

        async def rename():
            renamed = await env.doc_store.update_doc_metadata(doc["id"], title="Renamed")
            landed["updated_at"] = renamed["updated_at"]

        result = run_queued(
            doc["id"],
            lambda: ui_writes().add_asset_from_ui(
                env.users["alice"], doc["id"], "x.png", PNG,
            ),
            rename,
        )
        assert isinstance(result, dict), result
        assert landed["updated_at"] != queued_with
        assert result["previous_updated_at"] == landed["updated_at"]
        assert result["updated_at"] == token(env, doc["id"])

    def test_concurrent_uploads_at_the_cap_one_wins(self, env, monkeypatch):
        from chat.docs import service

        doc = seed_doc(env)
        files.add_asset(doc["id"], "existing.png", PNG)
        monkeypatch.setattr(constants, "DOC_MAX_ASSETS", 2)
        alice = env.users["alice"]

        async def both():
            return await asyncio.gather(
                ui_writes().add_asset_from_ui(alice, doc["id"], "a.png", PNG),
                ui_writes().add_asset_from_ui(alice, doc["id"], "b.png", PNG),
                return_exceptions=True,
            )

        results = _run(both())
        wins = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, service.DocRequestError)]
        assert len(wins) == 1 and len(refused) == 1, results
        assert refused[0].code == "asset_limit"
        assert wins[0]["asset_count"] == 2
        assert len(files.list_assets(doc["id"])) == 2
        assert row(env, doc["id"])["asset_count"] == 2

    @pytest.mark.parametrize("action", ["content", "upload", "delete"])
    def test_doc_deleted_after_route_check_404(self, env, monkeypatch, action):
        """Deleted between the route's visibility check and the write: the
        service's not-found becomes the route's 404 doc_not_found."""
        from chat.docs import routes

        doc = seed_doc(env)
        files.add_asset(doc["id"], "chart.png", PNG)
        real = routes._get_doc_for_ui

        async def check_then_delete(user, doc_id):
            found = await real(user, doc_id)
            await env.doc_store.delete_doc(doc_id)
            return found

        monkeypatch.setattr(routes, "_get_doc_for_ui", check_then_delete)
        c = client(env)
        if action == "content":
            resp = put_content(c, doc["id"], "x\n", token(env, doc["id"]))
        elif action == "upload":
            resp = upload(c, doc["id"], PNG, "new.png")
        else:
            resp = c.delete(f"/app/api/docs/{doc['id']}/assets/chart.png")
        assert resp.status_code == 404, resp.text
        assert detail(resp) == not_found(doc["id"])
        assert body(doc["id"]) == "line one\nline two\n"
        assert [a["name"] for a in files.list_assets(doc["id"])] == ["chart.png"]


# ---------------------------------------------------------------------------
# POST /docs/{id}/assets
# ---------------------------------------------------------------------------


class TestUploadAsset:
    def test_upload_201_shape_asset_only_write(self, env):
        doc = seed_doc(env)
        alice = uid(env, "alice")
        before = row(env, doc["id"])
        n_revs = len(revisions(doc["id"]))
        meta = files.read_doc_meta(doc["id"])
        env.published.clear()

        resp = upload(client(env), doc["id"], PNG, "chart.png")
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert set(data) == {
            "asset", "markdown", "asset_count", "updated_at", "previous_updated_at",
        }
        assert data["asset"] == {"name": "chart.png", "size": len(PNG), "mime": "image/png"}
        assert data["markdown"] == "![chart](assets/chart.png)"
        assert data["asset_count"] == 1
        assert data["previous_updated_at"] == before["updated_at"]
        assert data["updated_at"] != before["updated_at"]

        assert (asset_dir(env, doc["id"]) / "chart.png").read_bytes() == PNG
        # Asset-only: body, revisions and doc.meta.json untouched.
        assert body(doc["id"]) == "line one\nline two\n"
        assert len(revisions(doc["id"])) == n_revs
        assert files.read_doc_meta(doc["id"]) == meta
        after = row(env, doc["id"])
        assert after["updated_at"] == data["updated_at"]
        assert after["asset_count"] == 1
        assert after["content_size"] == before["content_size"]
        assert after["last_write_source"] == f"ui:{alice}"
        assert_write_events(env, alice, doc["id"], data["updated_at"])

        # The base router serves it.
        got = client(env).get(f"/app/api/docs/{doc['id']}/assets/chart.png")
        assert got.status_code == 200 and got.content == PNG

    def test_name_sanitized_and_suffixed(self, env):
        doc = seed_doc(env)
        c = client(env)
        first = upload(c, doc["id"], PNG, "My Chart!.png").json()
        second = upload(c, doc["id"], PNG, "My Chart!.png").json()
        assert first["asset"]["name"] == "My-Chart.png"
        assert second["asset"]["name"] == "My-Chart-2.png"
        assert second["markdown"] == "![My Chart!](assets/My-Chart-2.png)"
        assert second["asset_count"] == 2
        assert second["previous_updated_at"] == first["updated_at"]
        assert row(env, doc["id"])["asset_count"] == 2

    def test_extension_from_magic_bytes(self, env):
        doc = seed_doc(env)
        c = client(env)
        for data, filename, name, mime in (
            (GIF, "photo.png", "photo.gif", "image/gif"),
            (JPEG, "shot", "shot.jpg", "image/jpeg"),
            (WEBP, "w.bin", "w.webp", "image/webp"),
        ):
            resp = upload(c, doc["id"], data, filename)
            assert resp.status_code == 201, resp.text
            assert resp.json()["asset"] == {"name": name, "size": len(data), "mime": mime}

    def test_path_in_filename_is_basename(self, env):
        doc = seed_doc(env)
        resp = upload(client(env), doc["id"], PNG, "../../etc/evil.png")
        assert resp.status_code == 201
        assert resp.json()["asset"]["name"] == "evil.png"
        assert sorted(os.listdir(asset_dir(env, doc["id"]))) == ["evil.png"]

    def test_alt_escaped_and_defaulted(self, env):
        doc = seed_doc(env)
        c = client(env)
        resp = upload(c, doc["id"], PNG, "x.png", alt="a]b\n[c\\d `e <f> | g")
        assert resp.json()["markdown"] == (
            r"![a\]b \[c\\d \`e \<f\> \| g](assets/x.png)"
        )
        resp = upload(c, doc["id"], PNG, "y.png", alt="   ")
        assert resp.json()["markdown"] == "![y](assets/y.png)"
        resp = upload(c, doc["id"], PNG, "Q3 revenue.png", alt="Revenue by quarter")
        assert resp.json()["markdown"] == "![Revenue by quarter](assets/Q3-revenue.png)"

    def test_alt_truncated(self, env):
        cap = ui_writes().ALT_MAX_CHARS
        doc = seed_doc(env)
        resp = upload(client(env), doc["id"], PNG, "x.png", alt="a" * (cap + 50))
        assert resp.json()["markdown"] == "![" + "a" * cap + "](assets/x.png)"
        # Escapes are added after the cut, so they are never split.
        md = ui_writes().image_markdown("|" * (cap + 5), None, "x.png")
        assert md == "![" + "\\|" * cap + "](assets/x.png)"

    def test_alt_field_over_8_kib_refused(self, env):
        doc = seed_doc(env)
        resp = upload(client(env), doc["id"], PNG, "x.png", alt="a" * (8 * 1024 + 1))
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_request"
        assert files.list_assets(doc["id"]) == []

    @pytest.mark.parametrize("data,filename", [
        (SVG, "logo.svg"),
        (SVG, "logo.png"),
        (b"just some text\n", "notes.png"),
        (b"\x89PN", "short.png"),
        (b"", "empty.png"),
    ])
    def test_invalid_image(self, env, data, filename):
        doc = seed_doc(env)
        before = token(env, doc["id"])
        resp = upload(client(env), doc["id"], data, filename)
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_image"
        assert files.list_assets(doc["id"]) == []
        assert token(env, doc["id"]) == before

    def test_image_too_large(self, env, monkeypatch):
        monkeypatch.setattr(constants, "DOC_MAX_IMAGE_SIZE", len(PNG))
        doc = seed_doc(env)
        c = client(env)
        resp = upload(c, doc["id"], PNG + b"\x00", "big.png")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "image_too_large"
        assert files.list_assets(doc["id"]) == []
        # Exactly at the cap is fine.
        assert upload(c, doc["id"], PNG, "ok.png").status_code == 201

    def test_read_is_capped(self, env, monkeypatch):
        """The route never asks for more than cap + 1 bytes of the upload."""
        from starlette.datastructures import UploadFile

        monkeypatch.setattr(constants, "DOC_MAX_IMAGE_SIZE", len(PNG))
        sizes = []
        real_read = UploadFile.read

        async def spy(self, size=-1):
            sizes.append(size)
            return await real_read(self, size)

        monkeypatch.setattr(UploadFile, "read", spy)
        doc = seed_doc(env)
        assert upload(client(env), doc["id"], PNG, "ok.png").status_code == 201
        assert sizes == [len(PNG) + 1]

    def test_service_rejects_over_cap_data(self, env, monkeypatch):
        monkeypatch.setattr(constants, "DOC_MAX_IMAGE_SIZE", len(PNG))
        doc = seed_doc(env)
        from chat.docs import service

        with pytest.raises(service.DocRequestError) as exc:
            _run(ui_writes().add_asset_from_ui(
                env.users["alice"], doc["id"], "x.png", PNG + b"\x00",
            ))
        assert exc.value.code == "image_too_large"

    def test_asset_count_limit(self, env, monkeypatch):
        monkeypatch.setattr(constants, "DOC_MAX_ASSETS", 1)
        doc = seed_doc(env)
        c = client(env)
        assert upload(c, doc["id"], PNG, "one.png").status_code == 201
        before = token(env, doc["id"])
        resp = upload(c, doc["id"], PNG, "two.png")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "asset_limit"
        assert [a["name"] for a in files.list_assets(doc["id"])] == ["one.png"]
        assert token(env, doc["id"]) == before

    def test_asset_total_bytes_limit(self, env, monkeypatch):
        monkeypatch.setattr(constants, "DOC_MAX_ASSETS_TOTAL_BYTES", len(PNG) + 10)
        doc = seed_doc(env)
        c = client(env)
        assert upload(c, doc["id"], PNG, "one.png").status_code == 201
        resp = upload(c, doc["id"], PNG, "two.png")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "asset_limit"

    def test_access(self, env):
        doc = seed_doc(env)
        share(env, doc, "bob", "read")
        resp = upload(client(env, "bob"), doc["id"], PNG)
        assert resp.status_code == 403
        assert detail(resp)["error"] == "forbidden"

        resp = upload(client(env, "carol"), doc["id"], PNG)
        assert resp.status_code == 404
        assert detail(resp) == not_found(doc["id"])
        missing_id = str(uuid.uuid4())
        resp = upload(client(env, "carol"), missing_id, PNG)
        assert detail(resp) == not_found(missing_id)
        assert files.list_assets(doc["id"]) == []

        share(env, doc, "bob", "write")
        resp = upload(client(env, "bob"), doc["id"], PNG, "bobs.png")
        assert resp.status_code == 201
        assert row(env, doc["id"])["last_write_source"] == f"ui:{uid(env, 'bob')}"

    def test_malformed_forms(self, env):
        doc = seed_doc(env)
        c = client(env)
        url = f"/app/api/docs/{doc['id']}/assets"
        no_file = c.post(url, files={"alt": (None, "x")})
        assert no_file.status_code == 400
        assert detail(no_file) == {
            "error": "invalid_request",
            "message": "file is required (multipart field 'file').",
        }
        as_json = c.post(url, json={"file": "x"})
        assert as_json.status_code == 400
        assert detail(as_json)["error"] == "invalid_request"
        two_files = c.post(url, files=[
            ("file", ("a.png", PNG, "image/png")),
            ("file", ("b.png", PNG, "image/png")),
        ])
        assert two_files.status_code == 400
        assert detail(two_files)["error"] == "invalid_request"
        assert "Too many files" in detail(two_files)["message"]
        # ``alt`` sent as a file is a second file part: refused the same way.
        alt_as_file = c.post(url, files={
            "file": ("a.png", PNG, "image/png"),
            "alt": ("alt.txt", b"x", "text/plain"),
        })
        assert alt_as_file.status_code == 400
        assert "Too many files" in detail(alt_as_file)["message"]
        assert files.list_assets(doc["id"]) == []

    def test_urlencoded_body_refused_unread(self, env, monkeypatch):
        doc = seed_doc(env)
        pulled = spy_request_stream(monkeypatch)
        resp = client(env).post(
            f"/app/api/docs/{doc['id']}/assets", data={"file": "x" * 10_000},
        )
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_request"
        assert pulled == []

    def test_garbage_multipart_400(self, env):
        doc = seed_doc(env)
        resp = client(env).post(
            f"/app/api/docs/{doc['id']}/assets",
            content=b"--BOUND\r\nX\r\n\r\n\r\ngarbage--BOUND--",
            headers={"Content-Type": "multipart/form-data; boundary=BOUND"},
        )
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_request"

    def test_missing_boundary_400(self, env):
        doc = seed_doc(env)
        resp = client(env).post(
            f"/app/api/docs/{doc['id']}/assets",
            content=b"whatever",
            headers={"Content-Type": "multipart/form-data"},
        )
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_request"

    def test_declared_length_over_limit_refused_unread(self, env, monkeypatch):
        monkeypatch.setattr(constants, "DOC_MAX_IMAGE_SIZE", 100)
        doc = seed_doc(env)
        pulled = spy_request_stream(monkeypatch)
        big = PNG + b"\x00" * (70 * 1024)  # > 100 B + 64 KiB overhead
        resp = upload(client(env), doc["id"], big, "big.png")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "image_too_large"
        assert pulled == []
        assert files.list_assets(doc["id"]) == []

    def test_chunked_oversized_body_read_is_bounded(self, env, monkeypatch):
        """No Content-Length: the stream is cut once past the cap."""
        monkeypatch.setattr(constants, "DOC_MAX_IMAGE_SIZE", 1000)
        limit = 1000 + 64 * 1024
        doc = seed_doc(env)
        chunk = 16 * 1024
        status, data, pulled = _run(asgi_upload(
            env, doc["id"], multipart_chunks(PNG, chunk, total=4 * 1024 * 1024),
        ))
        assert status == 400
        assert data["detail"]["error"] == "image_too_large"
        assert limit < pulled <= limit + chunk + 512
        assert files.list_assets(doc["id"]) == []

    def test_chunked_body_within_cap_accepted(self, env):
        doc = seed_doc(env)
        status, data, _pulled = _run(asgi_upload(
            env, doc["id"], multipart_chunks(PNG, 16, total=len(PNG) + 100),
        ))
        assert status == 201, data
        assert data["asset"]["name"] == "chart.png"
        assert data["asset"]["size"] == len(PNG) + 100

    def test_spooled_files_closed_on_every_path(self, env, monkeypatch):
        import tempfile

        import starlette.formparsers as formparsers

        created = []

        class Recording(tempfile.SpooledTemporaryFile):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

        monkeypatch.setattr(formparsers, "SpooledTemporaryFile", Recording)
        doc = seed_doc(env)
        c = client(env)
        url = f"/app/api/docs/{doc['id']}/assets"

        assert upload(c, doc["id"], PNG, "ok.png").status_code == 201  # success
        assert upload(c, doc["id"], b"text", "bad.png").status_code == 400  # service refusal
        two = c.post(url, files=[                                       # MultiPartException
            ("file", ("a.png", PNG, "image/png")),
            ("file", ("b.png", PNG, "image/png")),
        ])
        assert two.status_code == 400
        broken = c.post(url, content=(                                   # library ValueError
            b'--BOUND\r\nContent-Disposition: form-data; name="file"; filename="a.png"'
            b"\r\n\r\n" + PNG + b"\r\n--BOUND\r\nno colon here\r\n\r\nx\r\n--BOUND--\r\n"
        ), headers={"Content-Type": "multipart/form-data; boundary=BOUND"})
        assert broken.status_code == 400
        assert detail(broken)["error"] == "invalid_request"
        monkeypatch.setattr(constants, "DOC_MAX_IMAGE_SIZE", 1000)
        status, _data, _pulled = _run(asgi_upload(                       # stream cut
            env, doc["id"], multipart_chunks(PNG, 4096, total=200 * 1024),
        ))
        assert status == 400
        assert len(created) >= 5
        assert all(f.closed for f in created)


# ---------------------------------------------------------------------------
# DELETE /docs/{id}/assets/{name}
# ---------------------------------------------------------------------------


class TestDeleteAsset:
    def test_owner_deletes_asset_only_write(self, env):
        doc = seed_doc(env)
        alice = uid(env, "alice")
        files.add_asset(doc["id"], "chart.png", PNG)
        files.add_asset(doc["id"], "other.png", PNG)
        before = row(env, doc["id"])
        n_revs = len(revisions(doc["id"]))
        meta = files.read_doc_meta(doc["id"])
        env.published.clear()

        resp = client(env).delete(f"/app/api/docs/{doc['id']}/assets/chart.png")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {
            "deleted": True,
            "asset_count": 1,
            "updated_at": data["updated_at"],
            "previous_updated_at": before["updated_at"],
        }
        assert data["updated_at"] != before["updated_at"]
        assert not (asset_dir(env, doc["id"]) / "chart.png").exists()
        assert [a["name"] for a in files.list_assets(doc["id"])] == ["other.png"]
        assert body(doc["id"]) == "line one\nline two\n"
        assert len(revisions(doc["id"])) == n_revs
        assert files.read_doc_meta(doc["id"]) == meta
        after = row(env, doc["id"])
        assert after["asset_count"] == 1
        assert after["content_size"] == before["content_size"]
        assert after["updated_at"] == data["updated_at"]
        assert after["last_write_source"] == f"ui:{alice}"
        assert_write_events(env, alice, doc["id"], data["updated_at"])

        again = client(env).delete(f"/app/api/docs/{doc['id']}/assets/chart.png")
        assert again.status_code == 404
        assert detail(again)["error"] == "asset_not_found"

    def test_owner_only(self, env):
        doc = seed_doc(env)
        files.add_asset(doc["id"], "chart.png", PNG)
        url = f"/app/api/docs/{doc['id']}/assets/chart.png"
        share(env, doc, "bob", "write")
        resp = client(env, "bob").delete(url)
        assert resp.status_code == 403
        assert detail(resp)["error"] == "forbidden"
        resp = client(env, "carol").delete(url)
        assert resp.status_code == 404
        assert detail(resp) == not_found(doc["id"])
        assert (asset_dir(env, doc["id"]) / "chart.png").exists()

    @pytest.mark.parametrize("name", [
        "..%2Fdoc.md",
        "..%2F..%2Fdoc.md",
        "%2E%2E%2Fdoc.md",
        ".hidden.png",
        "a/b.png",
        "a%2Fb.png",
        "doc.md",
        "nope.png",
        "chart",
        "%00.png",
    ])
    def test_bad_names_404_asset_not_found(self, env, name):
        doc = seed_doc(env)
        files.add_asset(doc["id"], "chart.png", PNG)
        assets = asset_dir(env, doc["id"])
        (assets / ".hidden.png").write_bytes(PNG)
        (assets / "a").mkdir()
        (assets / "a" / "b.png").write_bytes(PNG)
        before = token(env, doc["id"])

        resp = client(env).delete(f"/app/api/docs/{doc['id']}/assets/{name}")
        assert resp.status_code == 404, resp.text
        assert detail(resp)["error"] == "asset_not_found"
        assert body(doc["id"]) == "line one\nline two\n"
        assert (assets / ".hidden.png").exists()
        assert (assets / "a" / "b.png").exists()
        assert (assets / "chart.png").exists()
        assert token(env, doc["id"]) == before

    @pytest.mark.parametrize("name", ["../doc.md", "../../x.png", "/etc/passwd.png", ""])
    def test_service_refuses_traversal(self, env, name):
        from chat.docs import service

        doc = seed_doc(env)
        with pytest.raises(service.DocRequestError) as exc:
            _run(ui_writes().delete_asset_from_ui(env.users["alice"], doc["id"], name))
        assert exc.value.code == "asset_not_found"
        assert body(doc["id"]) == "line one\nline two\n"

    def test_symlink_not_followed(self, env):
        doc = seed_doc(env)
        assets = asset_dir(env, doc["id"])
        assets.mkdir(exist_ok=True)
        outside = env.tmp / "outside.png"
        outside.write_bytes(PNG)
        os.symlink(outside, assets / "link.png")
        os.symlink(env.dirs["docs"] / doc["id"] / "doc.md", assets / "body.png")

        c = client(env)
        for name in ("link.png", "body.png"):
            resp = c.delete(f"/app/api/docs/{doc['id']}/assets/{name}")
            assert resp.status_code == 404
            assert detail(resp)["error"] == "asset_not_found"
        assert outside.read_bytes() == PNG
        assert os.path.islink(assets / "link.png")
        assert os.path.islink(assets / "body.png")
        assert body(doc["id"]) == "line one\nline two\n"

    def test_in_use_409(self, env):
        doc = seed_doc(env, content="intro\n\n![c](assets/chart.png)\n")
        files.add_asset(doc["id"], "chart.png", PNG)
        before = token(env, doc["id"])
        resp = client(env).delete(f"/app/api/docs/{doc['id']}/assets/chart.png")
        assert resp.status_code == 409
        assert detail(resp)["error"] == "asset_in_use"
        assert (asset_dir(env, doc["id"]) / "chart.png").exists()
        assert token(env, doc["id"]) == before

    def test_in_use_checks_the_current_body_only(self, env):
        doc = seed_doc(env, content="![c](assets/chart.png)\n")
        files.add_asset(doc["id"], "chart.png", PNG)
        c = client(env)
        # Remove the reference; the old body survives only as a revision.
        put = put_content(c, doc["id"], "no images\n", token(env, doc["id"]))
        assert put.status_code == 200
        assert "assets/chart.png" in revisions(doc["id"])[-1].read_text()
        resp = c.delete(f"/app/api/docs/{doc['id']}/assets/chart.png")
        assert resp.status_code == 200
        assert resp.json()["previous_updated_at"] == put.json()["updated_at"]

    def test_similar_names_do_not_block(self, env):
        doc = seed_doc(
            env,
            content="![a](assets/chart.png.png)\n![b](assets/chart-2.png)\n"
                    "see myassets/chart.pngx\n",
        )
        for _ in range(2):
            files.add_asset(doc["id"], "chart.png", PNG)  # chart.png, chart-2.png
        files.add_asset(doc["id"], "chart.png.png", PNG)  # stem "chart.png"
        names = sorted(a["name"] for a in files.list_assets(doc["id"]))
        assert names == ["chart-2.png", "chart.png", "chart.png.png"]
        c = client(env)
        resp = c.delete(f"/app/api/docs/{doc['id']}/assets/chart.png")
        assert resp.status_code == 200, resp.text
        assert resp.json()["asset_count"] == 2
        for name in ("chart.png.png", "chart-2.png"):
            resp = c.delete(f"/app/api/docs/{doc['id']}/assets/{name}")
            assert resp.status_code == 409, name

    @pytest.mark.parametrize("text,referenced", [
        ("![x](assets/chart.png)", True),
        ('<img src="assets/chart.png">', True),
        ("assets/chart.png", True),
        ("![x](./assets/chart.png?v=2)", True),
        ("![x](assets/chart.png#frag)", True),
        ("![x](assets/chart.png.png)", False),
        ("![x](assets/chart.png-2)", False),
        ("![x](assets/chart.pngx)", False),
        ("![x](assets/chart_png)", False),
        ("![x](assets/chart-2.png)", False),
        ("![x](assets/Chart.png)", False),
        ("chart.png", False),
        # Escaped spellings that still resolve to the asset count as in use.
        ("![x](assets/ch%61rt.png)", True),
        ("![x](assets%2Fchart.png)", True),
        ("![x](assets/chart%2Epng)", True),
        ("![x](assets/chart&#46;png)", True),
        ("![x](assets&#x2F;chart.png)", True),
        ("![x](assets/chart\\.png)", True),
        ('<img src="assets/chart&period;png">', True),
        ("![x](assets/chart&#37;2Epng)", True),
        ("![x](assets/ch%61rt.png.png)", False),
    ])
    def test_asset_referenced(self, text, referenced):
        assert ui_writes().asset_referenced(text, "chart.png") is referenced

    @pytest.mark.parametrize("reference", [
        "assets/ch%61rt.png", "assets/chart\\.png", "assets/chart&#46;png",
        "assets%2Fchart.png",
    ])
    def test_escaped_reference_blocks_delete(self, env, reference):
        doc = seed_doc(env, content=f"![c]({reference})\n")
        files.add_asset(doc["id"], "chart.png", PNG)
        resp = client(env).delete(f"/app/api/docs/{doc['id']}/assets/chart.png")
        assert resp.status_code == 409
        assert detail(resp)["error"] == "asset_in_use"
        assert (asset_dir(env, doc["id"]) / "chart.png").exists()

    def test_read_share_recipient_403(self, env):
        doc = seed_doc(env)
        files.add_asset(doc["id"], "chart.png", PNG)
        share(env, doc, "bob", "read")
        resp = client(env, "bob").delete(f"/app/api/docs/{doc['id']}/assets/chart.png")
        assert resp.status_code == 403
        assert detail(resp)["error"] == "forbidden"
        assert (asset_dir(env, doc["id"]) / "chart.png").exists()


# ---------------------------------------------------------------------------
# files.delete_asset
# ---------------------------------------------------------------------------


class TestFilesDeleteAsset:
    def test_returns_new_count(self, env):
        doc = seed_doc(env)
        files.add_asset(doc["id"], "a.png", PNG)
        files.add_asset(doc["id"], "b.png", PNG)
        (asset_dir(env, doc["id"]) / ".hidden.png").write_bytes(PNG)
        assert files.delete_asset(doc["id"], "a.png") == 1
        assert files.asset_stats(doc["id"]) == (1, len(PNG))

    @pytest.mark.parametrize("name", [
        "../doc.md", "a/b.png", ".hidden.png", "doc.md", "x.svg", "", None, "a\\b.png",
        "a\x00.png", "missing.png",
    ])
    def test_bad_names(self, env, name):
        doc = seed_doc(env)
        with pytest.raises(files.DocFileError):
            files.delete_asset(doc["id"], name)
        assert body(doc["id"]) == "line one\nline two\n"

    def test_refuses_non_regular_leaves(self, env):
        doc = seed_doc(env)
        assets = asset_dir(env, doc["id"])
        (assets / "dir.png").mkdir()
        os.mkfifo(assets / "fifo.png")
        target = env.tmp / "target.png"
        target.write_bytes(PNG)
        os.symlink(target, assets / "link.png")
        os.symlink(env.tmp / "dangling.png", assets / "dangling.png")
        for name in ("dir.png", "fifo.png", "link.png", "dangling.png"):
            with pytest.raises(files.DocFileError):
                files.delete_asset(doc["id"], name)
        assert (assets / "dir.png").is_dir()
        assert os.path.lexists(assets / "fifo.png")
        assert os.path.islink(assets / "link.png") and target.read_bytes() == PNG
        assert os.path.islink(assets / "dangling.png")

    def test_symlinked_assets_dir_refused(self, env):
        doc = seed_doc(env)
        assets = asset_dir(env, doc["id"])
        elsewhere = env.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "x.png").write_bytes(PNG)
        os.rmdir(assets)
        os.symlink(elsewhere, assets)
        with pytest.raises(files.DocFileError):
            files.delete_asset(doc["id"], "x.png")
        assert (elsewhere / "x.png").read_bytes() == PNG

    def test_missing_assets_dir(self, env):
        doc = seed_doc(env)
        os.rmdir(asset_dir(env, doc["id"]))
        with pytest.raises(files.DocFileError):
            files.delete_asset(doc["id"], "x.png")


# ---------------------------------------------------------------------------
# Public-project docs and the gates
# ---------------------------------------------------------------------------


class TestPublicProjectDocs:
    def _public_doc(self, env):
        return seed_doc(
            env, "Open notes", content="![p](assets/keep.png)\n",
            mode="public", project_id=env.public_project,
        )

    def test_owner_edits_public_doc_from_ui(self, env):
        env.fg.set_feature_enabled(env.fg.FEATURE_PUBLIC_PROJECTS, True)
        doc = self._public_doc(env)
        c = client(env)
        resp = put_content(c, doc["id"], "edited by a person\n", token(env, doc["id"]))
        assert resp.status_code == 200, resp.text
        assert body(doc["id"]) == "edited by a person\n"
        up = upload(c, doc["id"], PNG, "pic.png")
        assert up.status_code == 201
        assert c.delete(f"/app/api/docs/{doc['id']}/assets/pic.png").status_code == 200

    def test_hidden_while_public_projects_gate_closed(self, env):
        doc = self._public_doc(env)  # gate closed by default in the fixture
        files.add_asset(doc["id"], "keep.png", PNG)
        files.add_asset(doc["id"], "spare.png", PNG)
        c = client(env)
        responses = [
            put_content(c, doc["id"], "x\n", token(env, doc["id"])),
            upload(c, doc["id"], PNG, "new.png"),
            c.delete(f"/app/api/docs/{doc['id']}/assets/spare.png"),
        ]
        for resp in responses:
            assert resp.status_code == 404
            assert detail(resp) == not_found(doc["id"])
        assert body(doc["id"]) == "![p](assets/keep.png)\n"
        assert sorted(a["name"] for a in files.list_assets(doc["id"])) == [
            "keep.png", "spare.png",
        ]

    def test_service_hides_too(self, env):
        from chat.docs import service

        doc = self._public_doc(env)
        with pytest.raises(service.DocError) as exc:
            _run(ui_writes().replace_body_from_ui(
                env.users["alice"], doc["id"], "x\n",
                expected_updated_at=token(env, doc["id"]),
            ))
        assert str(exc.value) == doc_not_found_message(doc["id"])


class TestDocsGate:
    def test_every_editing_route_403s_when_closed(self, env):
        doc = seed_doc(env)
        files.add_asset(doc["id"], "chart.png", PNG)
        env.fg.set_feature_enabled(env.fg.FEATURE_DOCS, False)
        c = client(env)
        responses = [
            put_content(c, doc["id"], "x\n", token(env, doc["id"])),
            upload(c, doc["id"], PNG),
            c.delete(f"/app/api/docs/{doc['id']}/assets/chart.png"),
        ]
        for resp in responses:
            assert resp.status_code == 403
            assert detail(resp) == {
                "error": "docs_disabled", "message": docs_disabled_message(),
            }
        assert body(doc["id"]) == "line one\nline two\n"
        assert [a["name"] for a in files.list_assets(doc["id"])] == ["chart.png"]
        assert env.published == []
