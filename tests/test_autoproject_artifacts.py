"""AutoProject docs and tables: the third ownership scope of Quest Docs and
Quest Tables (``docs.autoproject_id`` / ``data_tables.autoproject_id``).

- The access rule: an AutoProject artifact follows the matrix only from
  that AutoProject's runs and the UI; the owner's other private standalone
  conversations (another AutoProject's runs included) read it; project
  and public conversations, and every non-owner conversation, see nothing.
- Docs: ``create_doc(target="autoproject")`` only in a run, the
  ``autoproject`` list scope, free writes in the run and read-only
  outside, title uniqueness per AutoProject, the UI create and its
  not-found parity.
- Tables: the ``create_table`` card with ``target: "autoproject"`` (pre-card
  and execute), the list scope, row writes, the UI create.
- Deleting the AutoProject removes its docs and tables, rows and
  directories, and nothing else.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from chat import artifact_access
from chat.action_request_types import table_precard
from chat.action_request_types.create_table import CreateTableHandler
from chat.autoprojects import service as autoproject_service
from chat.autoprojects.constants import autoproject_not_found_message
from chat.docs import access as doc_access
from chat.docs import service as doc_service
from chat.tables import access as table_access
from chat.tables import service as table_service
from db import (
    autoproject_store,
    conversation_store,
    doc_store,
    project_store,
    table_store,
    user_store,
)

OWNER = 1
RECIPIENT = 2
AP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_AP = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PROJECT = "11111111-1111-4111-8111-111111111111"
COLUMNS = [{"name": "Name", "type": "text"}]


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The access rule (pure)
# ---------------------------------------------------------------------------


def _item(shares=(), *, require_approval=False, autoproject_id=AP):
    return {
        "id": "33333333-3333-4333-8333-333333333333",
        "owner_id": OWNER,
        "project_id": None,
        "autoproject_id": autoproject_id,
        "mode": "private",
        "require_approval": require_approval,
        "shares": [
            {"id": i, "user_id": user_id, "permission": permission}
            for i, (user_id, permission) in enumerate(shares, start=1)
        ],
    }


def _core(item, *, user_id=OWNER, run_kind="top_level", autoproject_id=None,
          project_id=None, is_public=False):
    return artifact_access.resolve_core(
        item, user_id=user_id, is_public=is_public, project_id=project_id,
        run_kind=run_kind, autoproject_id=autoproject_id,
    )


def test_own_run_follows_the_matrix():
    assert _core(_item(), autoproject_id=AP).write == "free"
    shared = _item(shares=[(RECIPIENT, "read")])
    assert _core(shared, autoproject_id=AP).write == "approval"
    gated = _core(_item(require_approval=True), autoproject_id=AP)
    assert (gated.write, gated.required_by_owner) == ("approval", True)


@pytest.mark.parametrize("run_kind", sorted(artifact_access.READ_ONLY_RUN_KINDS))
def test_read_only_run_kinds_of_the_run_read(run_kind):
    verdict = _core(_item(), run_kind=run_kind, autoproject_id=AP)
    assert (verdict.visible, verdict.write, verdict.cause) == (True, "denied", run_kind)


@pytest.mark.parametrize("caller_autoproject", [None, OTHER_AP])
def test_owner_outside_the_autoproject_reads_only(caller_autoproject):
    verdict = _core(_item(), autoproject_id=caller_autoproject)
    assert (verdict.visible, verdict.write, verdict.cause) == (
        True, "denied", "outside_autoproject",
    )
    # A read-only run kind outside keeps its own cause.
    sub = _core(_item(), run_kind="sub_agent", autoproject_id=caller_autoproject)
    assert (sub.visible, sub.cause) == (True, "sub_agent")
    slack = _core(_item(), run_kind="slack", autoproject_id=caller_autoproject)
    assert (slack.visible, slack.cause) == (True, "outside_autoproject")


def test_project_and_public_conversations_see_nothing():
    assert not _core(_item(), project_id=PROJECT).visible
    assert not _core(_item(), project_id=PROJECT, is_public=True).visible
    assert not _core(_item(), is_public=True).visible


def test_share_recipients_reach_it_in_the_ui_only():
    shared = _item(shares=[(RECIPIENT, "write")])
    assert not _core(shared, user_id=RECIPIENT).visible
    assert not _core(shared, user_id=RECIPIENT, autoproject_id=OTHER_AP).visible
    ui = _core(shared, user_id=RECIPIENT, run_kind="ui")
    assert (ui.visible, ui.write) == (True, "free")
    assert not _core(_item(), user_id=RECIPIENT, run_kind="ui").visible


def test_ui_owner_writes_freely_and_user_artifacts_are_unchanged():
    assert _core(_item(), run_kind="ui").write == "free"
    # An artifact with no AutoProject is untouched by the caller's run.
    plain = _item(autoproject_id=None)
    assert _core(plain, autoproject_id=AP).write == "free"
    assert _core(plain).write == "free"
    # A dict without the key at all (older callers) is a user artifact.
    del plain["autoproject_id"]
    assert _core(plain).write == "free"


def test_resolvers_word_the_outside_cause():
    doc = doc_access.resolve_doc_access(
        _item(), user_id=OWNER, is_public=False, project_id=None, run_kind="top_level",
    )
    assert doc.deny_reason == doc_access.DENY_OUTSIDE_AUTOPROJECT
    assert doc.deny_cause == "outside_autoproject"
    table = table_access.resolve_table_access(
        _item(), user_id=OWNER, is_public=False, project_id=None, run_kind="top_level",
    )
    assert table.deny_reason == table_access.DENY_OUTSIDE_AUTOPROJECT
    # Read-only on the table as a whole: the schema is denied the same way.
    assert (table.schema, table.schema_deny_reason) == (
        "denied", table_access.DENY_OUTSIDE_AUTOPROJECT,
    )
    in_run = table_access.resolve_table_access(
        _item(), user_id=OWNER, is_public=False, project_id=None, run_kind="top_level",
        autoproject_id=AP,
    )
    assert (in_run.rows, in_run.schema) == ("free", "approval")


# ---------------------------------------------------------------------------
# Services, against a database
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(isolated_db, tmp_path, monkeypatch):
    import chat.storage as storage_mod
    import config.feature_gates as fg
    import config.model_selection as model_selection
    from chat.realtime.bus import bus

    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", tmp_path / "feature_gates.json")
    for feature in (fg.FEATURE_DOCS, fg.FEATURE_TABLES, fg.FEATURE_AUTOPROJECTS):
        fg.set_feature_enabled(feature, True)
    for name in ("CHATS_DIR", "PROJECTS_DIR", "DOCS_DIR", "TABLES_DIR", "AUTOPROJECTS_DIR"):
        path = tmp_path / name.lower()
        path.mkdir()
        monkeypatch.setattr(storage_mod, name, path)
    monkeypatch.setattr(model_selection, "is_model_allowed", lambda *a, **kw: True)
    published: list[tuple[int, dict]] = []
    monkeypatch.setattr(bus, "publish_to_user", lambda uid, ev: published.append((uid, ev)))
    monkeypatch.setattr(bus, "publish_to_conversation", lambda cid, ev: None)

    async def _seed():
        owner = await user_store.create_user("owner@example.com", "Owner", "key-owner")
        other = await user_store.create_user("other@example.com", "Other", "key-other")
        first = await autoproject_store.create_autoproject(owner["id"], title="Research")
        second = await autoproject_store.create_autoproject(owner["id"], title="Launch")
        theirs = await autoproject_store.create_autoproject(other["id"], title="Theirs")
        project = await project_store.create_project(owner["id"], "Project")
        return owner, other, first, second, theirs, project

    owner, other, first, second, theirs, project = _run(_seed())
    return SimpleNamespace(
        owner=owner, other=other, first=first, second=second, theirs=theirs,
        project=project, fg=fg, tmp=tmp_path, published=published,
    )


def _doc_caller(user, *, autoproject=None, run_kind="top_level", project_id=None,
                conversation_id="c-1"):
    return doc_service.Caller(
        user=user, conversation_id=None if run_kind == "ui" else conversation_id,
        project_id=project_id, is_public=False, run_kind=run_kind,
        autoproject_id=autoproject["id"] if autoproject else None,
    )


def _table_caller(user, *, autoproject=None, run_kind="top_level", project_id=None):
    return table_service.Caller(
        user=user, conversation_id=None if run_kind == "ui" else "c-1",
        project_id=project_id, is_public=False, run_kind=run_kind,
        autoproject_id=autoproject["id"] if autoproject else None,
    )


def _titles(rows) -> list[str]:
    return sorted(row["title"] for row in rows)


def test_create_doc_target_autoproject_only_in_a_run(env):
    run = _doc_caller(env.owner, autoproject=env.first)
    created = _run(doc_service.create_doc(run, "Findings", "# Findings\n", target="autoproject"))
    assert created["scope"] == "autoproject"
    assert created["autoproject_id"] == env.first["id"]
    assert (created["mode"], created["project_id"]) == ("private", None)
    stored = _run(doc_store.get_doc(created["id"]))
    assert stored["autoproject_id"] == env.first["id"]

    with pytest.raises(doc_service.DocError, match="only available inside an AutoProject run"):
        _run(doc_service.create_doc(
            _doc_caller(env.owner), "Elsewhere", "", target="autoproject",
        ))
    # A run of somebody else's AutoProject id reads as not found.
    foreign = _doc_caller(env.owner, autoproject=env.theirs)
    with pytest.raises(doc_service.DocError) as exc:
        _run(doc_service.create_doc(foreign, "Sneaky", "", target="autoproject"))
    assert str(exc.value) == autoproject_not_found_message(env.theirs["id"])


def test_doc_titles_are_unique_per_autoproject(env):
    first = _doc_caller(env.owner, autoproject=env.first)
    second = _doc_caller(env.owner, autoproject=env.second)
    _run(doc_service.create_doc(first, "Plan", "", target="autoproject"))
    with pytest.raises(doc_service.DocError, match="in this AutoProject"):
        _run(doc_service.create_doc(first, "plan", "", target="autoproject"))
    # The same title is free in another AutoProject and as a user doc.
    _run(doc_service.create_doc(second, "Plan", "", target="autoproject"))
    _run(doc_service.create_doc(first, "Plan", "", target="user"))
    with pytest.raises(doc_store.DocValidationError):
        _run(doc_store.create_doc(
            env.owner["id"], "Both", mode="private",
            project_id=env.project["id"], autoproject_id=env.first["id"],
        ))


def test_doc_list_scopes(env):
    first = _doc_caller(env.owner, autoproject=env.first)
    second = _doc_caller(env.owner, autoproject=env.second)
    chat = _doc_caller(env.owner)
    _run(doc_service.create_doc(first, "First doc", "", target="autoproject"))
    _run(doc_service.create_doc(second, "Second doc", "", target="autoproject"))
    _run(doc_service.create_doc(chat, "User doc", "", target="user"))

    def listed(caller, scope):
        return _run(doc_service.list_docs(caller, scope=scope))

    # In a run: its own AutoProject under "autoproject", everything under "all".
    own = listed(first, "autoproject")
    assert _titles(own) == ["First doc"]
    assert (own[0]["scope"], own[0]["writable"]) == ("autoproject", "free")
    assert _titles(listed(first, "all")) == ["First doc", "Second doc", "User doc"]
    other = next(r for r in listed(first, "all") if r["title"] == "Second doc")
    assert other["writable"] == "denied"
    assert other["write_note"] == doc_access.DENY_OUTSIDE_AUTOPROJECT
    # A user doc never appears as an AutoProject doc, or the reverse.
    assert _titles(listed(first, "user")) == ["User doc"]
    assert "autoproject_id" not in listed(first, "user")[0]

    # Any other standalone conversation: all of them, read-only.
    assert _titles(listed(chat, "autoproject")) == ["First doc", "Second doc"]
    assert {r["writable"] for r in listed(chat, "autoproject")} == {"denied"}
    assert _titles(listed(chat, "user")) == ["User doc"]

    # A project conversation and another user see none of them.
    in_project = _doc_caller(env.owner, project_id=env.project["id"])
    assert listed(in_project, "autoproject") == []
    assert _titles(listed(in_project, "all")) == ["User doc"]
    assert listed(_doc_caller(env.other), "all") == []


def test_doc_writes_free_in_the_run_and_refused_outside(env):
    run = _doc_caller(env.owner, autoproject=env.first)
    created = _run(doc_service.create_doc(run, "Log", "one\n", target="autoproject"))
    _run(doc_service.append_to_doc(run, created["id"], "two\n"))
    sub_agent = _doc_caller(env.owner, autoproject=env.first, run_kind="sub_agent")
    assert "two" in _run(doc_service.read_doc(sub_agent, created["id"]))["content"]

    for outsider in (_doc_caller(env.owner), _doc_caller(env.owner, autoproject=env.second)):
        _run(doc_service.read_doc(outsider, created["id"]))
        with pytest.raises(doc_service.DocError) as exc:
            _run(doc_service.append_to_doc(outsider, created["id"], "three\n"))
        assert str(exc.value) == doc_access.DENY_OUTSIDE_AUTOPROJECT
    in_project = _doc_caller(env.owner, project_id=env.project["id"])
    with pytest.raises(doc_service.DocError, match="not found"):
        _run(doc_service.read_doc(in_project, created["id"]))


def test_ui_doc_create_and_not_found_parity(env):
    doc = _run(doc_service.create_doc_from_ui(
        env.owner, "From the page", autoproject_id=env.first["id"],
    ))
    assert (doc["autoproject_id"], doc["mode"], doc["project_id"]) == (
        env.first["id"], "private", None,
    )

    def refused(user, autoproject_id) -> tuple[str, str]:
        with pytest.raises(doc_service.DocRequestError) as exc:
            _run(doc_service.create_doc_from_ui(user, "Nope", autoproject_id=autoproject_id))
        return exc.value.code, str(exc.value)

    missing = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    assert refused(env.owner, missing) == (
        "autoproject_not_found", autoproject_not_found_message(missing),
    )
    assert refused(env.owner, env.theirs["id"]) == (
        "autoproject_not_found", autoproject_not_found_message(env.theirs["id"]),
    )
    # Gate closed for the user: the same body as a missing AutoProject.
    env.fg.set_feature_enabled(env.fg.FEATURE_AUTOPROJECTS, False)
    assert refused(env.owner, env.first["id"])[0] == "autoproject_not_found"
    env.fg.set_feature_enabled(env.fg.FEATURE_AUTOPROJECTS, True)
    with pytest.raises(doc_service.DocRequestError) as exc:
        _run(doc_service.create_doc_from_ui(
            env.owner, "Both", project_id=env.project["id"],
            autoproject_id=env.first["id"],
        ))
    assert exc.value.code == "invalid_request"

    # The UI streams: "your docs" leaves it out, the AutoProject stream has it.
    mine = _run(doc_store.list_accessible_docs(env.owner["id"], owned_only=True))
    assert mine == []
    stream = _run(doc_store.list_accessible_docs(
        env.owner["id"], include_user_docs=False,
        autoproject_id=env.first["id"], include_autoproject_docs=True,
    ))
    assert _titles(stream) == ["From the page"]


async def _start_run(user, autoproject) -> dict:
    cid = await autoproject_service.create_run_conversation(user, autoproject["id"])
    meta = await conversation_store.get_conversation_meta(user["id"], cid)
    return (await autoproject_service.start_run(
        user, meta, prompt="Do it", model="fake-model",
    )).run


def test_create_table_card_targets_the_runs_autoproject(env):
    run = _run(_start_run(env.owner, env.first))
    handler = CreateTableHandler()
    params = handler.validate_params(
        {"title": "Leads", "target": "autoproject", "columns": COLUMNS},
    )
    _run(table_precard.precard_create_table(
        params, env.owner, None, run["conversation_id"],
    ))
    assert params["autoproject_title"] == "Research"
    assert params["table_mode"] == "private"
    preview = _run(handler.render_preview(params, env.owner))
    assert {"key": "AutoProject", "value": "Research"} in preview

    result = _run(handler.execute(
        params, env.owner, conversation_id=run["conversation_id"], request_id=7,
    ))
    assert (result["scope"], result["autoproject_id"]) == ("autoproject", env.first["id"])
    assert _run(table_store.get_table(result["table_id"]))["autoproject_id"] == env.first["id"]

    # The same title again collides inside the AutoProject only.
    again = handler.validate_params(
        {"title": "leads", "target": "autoproject", "columns": COLUMNS},
    )
    with pytest.raises(ValueError, match="in this AutoProject"):
        _run(table_precard.precard_create_table(
            again, env.owner, None, run["conversation_id"],
        ))
    # Outside a run the target is refused before a card opens.
    plain = handler.validate_params(
        {"title": "Leads", "target": "autoproject", "columns": COLUMNS},
    )
    with pytest.raises(ValueError, match="needs an AutoProject run"):
        _run(table_precard.precard_create_table(plain, env.owner, None, "c-plain"))


def test_table_scopes_row_writes_and_ui_create(env):
    ui = table_service.caller_for_ui(env.owner)
    table = _run(table_service.create_table(
        ui, title="Tracker", autoproject_id=env.first["id"], columns=COLUMNS,
        write_source=f"ui:{env.owner['id']}", actor_user_id=env.owner["id"],
    ))
    assert (table["autoproject_id"], table["mode"]) == (env.first["id"], "private")
    _run(table_service.create_table(
        ui, title="Tracker", columns=COLUMNS,
        write_source=f"ui:{env.owner['id']}", actor_user_id=env.owner["id"],
    ))
    with pytest.raises(table_service.TableError) as exc:
        _run(table_service.create_table(
            ui, title="Nope", autoproject_id=env.theirs["id"], columns=COLUMNS,
        ))
    assert exc.value.code == "autoproject_not_found"

    run = _table_caller(env.owner, autoproject=env.first)
    chat = _table_caller(env.owner)

    def listed(caller, scope):
        return _run(table_service.list_visible_tables(caller, scope=scope))

    own = listed(run, "autoproject")
    assert [(t["id"], access.rows) for t, access in own] == [(table["id"], "free")]
    assert len(listed(run, "user")) == 1
    assert listed(run, "user")[0][0]["autoproject_id"] is None
    outside = listed(chat, "autoproject")
    assert [(t["id"], access.rows) for t, access in outside] == [(table["id"], "denied")]
    assert listed(_table_caller(env.owner, project_id=env.project["id"]), "autoproject") == []

    _run(table_service.apply_row_write(
        run, table["id"], "insert", {"rows": [{"Name": "Ada"}]},
    ))
    with pytest.raises(table_service.TableError) as exc:
        _run(table_service.apply_row_write(
            chat, table["id"], "insert", {"rows": [{"Name": "Bob"}]},
        ))
    assert str(exc.value) == table_access.DENY_OUTSIDE_AUTOPROJECT
    assert _run(table_store.get_table(table["id"]))["row_count"] == 1


def test_deleting_the_autoproject_removes_its_docs_and_tables(env):
    import chat.storage as storage_mod

    run = _doc_caller(env.owner, autoproject=env.first)
    ui = table_service.caller_for_ui(env.owner)
    doc = _run(doc_service.create_doc(run, "Gone", "body", target="autoproject"))
    kept_doc = _run(doc_service.create_doc(run, "Kept", "body", target="user"))
    other_doc = _run(doc_service.create_doc_from_ui(
        env.owner, "Other", autoproject_id=env.second["id"],
    ))
    table = _run(table_service.create_table(
        ui, title="Gone", autoproject_id=env.first["id"], columns=COLUMNS,
    ))
    kept_table = _run(table_service.create_table(ui, title="Kept", columns=COLUMNS))
    doc_dir = storage_mod.DOCS_DIR / doc["id"]
    table_dir = storage_mod.TABLES_DIR / table["id"]
    assert doc_dir.is_dir() and table_dir.is_dir()
    env.published.clear()

    _run(autoproject_service.delete_autoproject(env.owner, env.first["id"]))

    assert _run(doc_store.get_doc(doc["id"])) is None
    assert _run(table_store.get_table(table["id"])) is None
    assert not doc_dir.exists() and not table_dir.exists()
    assert _run(doc_store.get_doc(kept_doc["id"])) is not None
    assert _run(doc_store.get_doc(other_doc["id"])) is not None
    assert _run(table_store.get_table(kept_table["id"])) is not None
    assert (storage_mod.DOCS_DIR / kept_doc["id"]).is_dir()
    kinds = {ev.get("type") for _uid, ev in env.published}
    assert {"doc_list_changed", "table_list_changed", "autoproject_list_changed"} <= kinds
