"""Every tool that writes or reads workspace files by bare path resolves
against the CONVERSATION workspace root, also in a project conversation
(devplan 00009 sections 3 and 6.1).

One parametrised test over the file-producing / file-reading callers of
``conversation_workspace_dir``: each runs with
``project_id`` set (a project conversation), upstream calls mocked, and
must touch ``ChatStorage.get_conversation_workspace_root(cid)`` only --
the project workspace resolver is made to blow up, and the project dir must
not exist afterwards.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import chat.storage as storage_mod

CID = "conv"
PID = "proj"
USER = {"id": 42, "email": "u@example.com"}


def _run(coro):
    return asyncio.run(coro)


def _response(content: bytes, content_type: str = "application/octet-stream"):
    resp = MagicMock()
    resp.status_code = 200
    resp.content = content
    resp.text = content.decode("utf-8", errors="replace")
    resp.headers = {"content-type": content_type}
    return resp


@pytest.fixture()
def env(tmp_path, monkeypatch):
    chats = tmp_path / "chats"
    projects = tmp_path / "projects"
    monkeypatch.setattr(storage_mod, "CHATS_DIR", chats, raising=True)
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", projects, raising=True)

    def _no_project_root(project_id):
        raise AssertionError("project workspace root must not be resolved")

    monkeypatch.setattr(
        storage_mod.ChatStorage, "get_project_workspace_root",
        staticmethod(_no_project_root),
    )
    from chat.realtime import bus
    events = []
    monkeypatch.setattr(bus, "publish_to_user", lambda uid, ev: events.append(ev))
    return SimpleNamespace(
        conv_root=storage_mod.ChatStorage.get_conversation_workspace_root(CID),
        projects=projects,
        events=events,
    )


# Each case returns the conversation-root-relative path it wrote / read.


async def _authed_get_output_file(env):
    from chat.gemini_api.authed_get import handle_authed_get
    with patch(
        "chat.gemini_api.authed_get._make_authed_request",
        new_callable=AsyncMock, return_value=_response(b'{"a":1}', "application/json"),
    ):
        out = json.loads(await handle_authed_get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            user=USER, conversation_id=CID, project_id=PID,
            output_file="dump.json",
        ))
    assert out["status"] == "success", out
    return out["path"]


async def _download_drive_file(env):
    from chat.gemini_api.tool_handlers import _handle_download_drive_file
    replies = [json.dumps({"name": "sheet.bin"}), _response(b"bytes")]
    with patch(
        "chat.gemini_api.authed_get._make_authed_request",
        new=AsyncMock(side_effect=replies),
    ):
        out = json.loads(await _handle_download_drive_file(
            USER, CID, "FILE1", project_id=PID,
        ))
    assert out["status"] == "success", out
    return out["filename"]


async def _google_export_doc(env):
    from chat.gemini_api.tool_handlers import _handle_google_export_doc
    replies = [
        json.dumps({"name": "Plan", "mimeType": "application/vnd.google-apps.document"}),
        _response(b"plain text", "text/plain"),
    ]
    with patch(
        "chat.gemini_api.authed_get._make_authed_request",
        new=AsyncMock(side_effect=replies),
    ):
        out = json.loads(await _handle_google_export_doc(
            USER, CID, "DOC1", "txt", project_id=PID,
        ))
    assert out["status"] == "success", out
    return out["filename"]


async def _io_resolve_workspace_file(env):
    from chat.action_request_types._io_attachments import resolve_workspace_file
    env.conv_root.mkdir(parents=True, exist_ok=True)
    (env.conv_root / "att.txt").write_text("hi")
    path = await resolve_workspace_file(CID, PID, "att.txt")
    return str(path.relative_to(env.conv_root.resolve()))


async def _io_read_workspace_attachments(env):
    from chat.action_request_types._io_attachments import read_workspace_attachments
    env.conv_root.mkdir(parents=True, exist_ok=True)
    (env.conv_root / "deck.pdf").write_bytes(b"%PDF")
    [(filename, data, _mime)] = await read_workspace_attachments(
        CID, PID, [{"path": "deck.pdf"}],
    )
    assert data == b"%PDF"
    return filename


async def _github_job_log(env):
    from plugins.github.tools import _handle_github_get_job_log
    ctx = SimpleNamespace(user=USER, conversation_id=CID, project_id=PID)
    with patch(
        "chat.gemini_api.authed_get._make_authed_request",
        new_callable=AsyncMock, return_value=_response(b"log line\n", "text/plain"),
    ):
        out = json.loads(await _handle_github_get_job_log(
            ctx, {"owner": "o", "repo": "r", "job_id": "5"},
        ))
    assert out["status"] == "success", out
    return out["path"]


async def _m365_save_mail_attachment(env, monkeypatch):
    import plugins.m365.tools as m365_tools

    meta = MagicMock(status_code=200)
    meta.json.return_value = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": "invoice.pdf", "size": 4,
    }
    value = MagicMock(status_code=200, content=b"%PDF")
    monkeypatch.setattr(
        m365_tools, "graph_request", AsyncMock(side_effect=[meta, value]),
    )
    ctx = SimpleNamespace(user=USER, conversation_id=CID, project_id=PID)
    out = json.loads(await m365_tools._handle_save_mail_attachment(
        ctx, {"message_id": "M1", "attachment_id": "A1"},
    ))
    assert out["status"] == "success", out
    return out["path"]


async def _unifi_camera_snapshot(env, monkeypatch):
    import plugins.unifi.tools as unifi_tools
    from plugins.unifi.tests.fake_controller import (
        HOST, NETWORK_KEY, PROTECT_KEY, FakeController,
    )

    fake = FakeController()
    monkeypatch.setattr(
        unifi_tools, "load_unifi_config",
        lambda *a, **k: {"host": HOST, "verify_tls": False},
    )
    monkeypatch.setattr(unifi_tools, "make_client", fake.make_client)
    provider = MagicMock()
    provider.upload_file = AsyncMock(return_value=None)
    user = {**USER, "service_credentials": {"unifi": {"oauth_blob": {
        "network_api_key": NETWORK_KEY, "protect_api_key": PROTECT_KEY,
    }}}}
    ctx = SimpleNamespace(
        user=user, conversation_id=CID, project_id=PID,
        provider=provider, model="claude-opus-4-8",
    )
    result = await unifi_tools._tool_get_camera_snapshot(ctx, {"camera": "cam-1"})
    out = json.loads(result[0] if isinstance(result, tuple) else result)
    assert out["status"] == "success", out
    return out["path"]


async def _google_export_sheet(env):
    from chat.gemini_api.tool_handlers import _handle_google_export_sheet
    replies = [
        json.dumps({"name": "Budget", "mimeType": "application/vnd.google-apps.spreadsheet"}),
        _response(b"a,b\n", "text/csv"),
    ]
    with patch(
        "chat.gemini_api.authed_get._make_authed_request",
        new=AsyncMock(side_effect=replies),
    ):
        out = json.loads(await _handle_google_export_sheet(
            USER, CID, "SHEET1", "csv", project_id=PID,
        ))
    assert out["status"] == "success", out
    return out["filename"]


async def _google_export_slides(env):
    from chat.gemini_api.tool_handlers import _handle_google_export_slides
    replies = [
        json.dumps({"name": "Deck", "mimeType": "application/vnd.google-apps.presentation"}),
        _response(b"slide text", "text/plain"),
    ]
    with patch(
        "chat.gemini_api.authed_get._make_authed_request",
        new=AsyncMock(side_effect=replies),
    ):
        out = json.loads(await _handle_google_export_slides(
            USER, CID, "DECK1", "txt", project_id=PID,
        ))
    assert out["status"] == "success", out
    return out["filename"]


async def _slack_dm_to_self_files(env, monkeypatch):
    import plugins.slack.tools as slack_tools

    env.conv_root.mkdir(parents=True, exist_ok=True)
    (env.conv_root / "chart.png").write_bytes(b"png-bytes")
    sent = {}

    async def fake_send(user, text, files=None):
        sent["files"] = files
        return {"ok": True}

    monkeypatch.setattr(slack_tools.upstream, "send_dm_to_self", fake_send)
    ctx = SimpleNamespace(user=USER, conversation_id=CID, project_id=PID)
    out = json.loads(await slack_tools._tool_send_slack_dm_to_self(
        ctx, {"message": "hi", "files": ["chart.png"]},
    ))
    assert "error" not in out, out
    assert sent["files"] == [("chart.png", b"png-bytes")]
    return "chart.png"


async def _gmail_draft_and_self_send_attachments(env, monkeypatch):
    # create_draft and send_email_to_self both resolve workspace
    # attachments through _resolve_request_attachments.
    import api.gmail.draft_endpoints as draft_endpoints
    from api.gmail.models import DraftAttachment

    async def fake_owned(user_id, conversation_id):
        return {"id": conversation_id, "project_id": PID}

    monkeypatch.setattr(draft_endpoints, "require_owned_conversation", fake_owned)
    env.conv_root.mkdir(parents=True, exist_ok=True)
    (env.conv_root / "memo.pdf").write_bytes(b"%PDF memo")
    [resolved] = await draft_endpoints._resolve_request_attachments(
        USER, [DraftAttachment(type="workspace", workspace_path="memo.pdf")], CID,
    )
    assert resolved["data"] == b"%PDF memo"
    return resolved["filename"]


async def _add_doc_image_source(env):
    from chat.docs.service import Caller, _load_workspace_image

    env.conv_root.mkdir(parents=True, exist_ok=True)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    (env.conv_root / "fig.png").write_bytes(png)
    caller = Caller(
        user=USER, conversation_id=CID, project_id=PID,
        is_public=False, run_kind="top_level",
    )
    data, name = await _load_workspace_image(caller, "fig.png")
    assert data == png
    return name


async def _subagent_return_landing(env, monkeypatch):
    import db.tool_wait_handle_store as wh_store
    import db.user_subagent_run_store as run_store
    from chat import user_subagent
    from chat.action_request_types.subagent_return import SubagentReturnHandler

    sub_root = storage_mod.ChatStorage.get_conversation_workspace_root("subconv")
    sub_root.mkdir(parents=True)
    (sub_root / "answer.md").write_text("result")
    run = {
        "id": "run-1", "status": "running", "wait_handle_id": "wh-1",
        "caller_conversation_id": CID, "caller_user_id": USER["id"],
    }

    async def _get_run(conversation_id):
        return dict(run)

    async def _get_handle(handle_id):
        return {"id": handle_id, "status": "pending"}

    async def _update_status(run_id, status, error=None):
        return {**run, "status": status}

    async def _resolve_caller(run_row, new_status, response):
        return {"id": "wh-1", "status": new_status}

    monkeypatch.setattr(run_store, "get_run_by_subagent_conversation", _get_run)
    monkeypatch.setattr(run_store, "update_run_status", _update_status)
    monkeypatch.setattr(wh_store, "get_handle", _get_handle)
    monkeypatch.setattr(user_subagent, "resolve_caller_handle", _resolve_caller)
    result = await SubagentReturnHandler().execute(
        {"response": "done", "files": ["answer.md"]},
        {"id": 2, "email": "b@example.com"},
        conversation_id="subconv",
    )
    assert result["files_returned"] == [".subagent_responses/answer.md"]
    return ".subagent_responses/answer.md"


CASES = [
    pytest.param(_authed_get_output_file, id="authed_get_output_file"),
    pytest.param(_download_drive_file, id="download_drive_file"),
    pytest.param(_google_export_doc, id="google_export_doc"),
    pytest.param(_io_resolve_workspace_file, id="io_attachments_resolve"),
    pytest.param(_io_read_workspace_attachments, id="io_attachments_read"),
    pytest.param(_github_job_log, id="github_get_job_log"),
    pytest.param(_m365_save_mail_attachment, id="m365_save_mail_attachment"),
    pytest.param(_unifi_camera_snapshot, id="unifi_get_camera_snapshot"),
    pytest.param(_google_export_sheet, id="google_export_sheet"),
    pytest.param(_google_export_slides, id="google_export_slides"),
    pytest.param(_slack_dm_to_self_files, id="send_slack_dm_to_self_files"),
    pytest.param(
        _gmail_draft_and_self_send_attachments,
        id="gmail_draft_and_self_send_attachments",
    ),
    pytest.param(_add_doc_image_source, id="add_doc_image_source"),
    pytest.param(_subagent_return_landing, id="subagent_return_landing"),
]


@pytest.mark.parametrize("case", CASES)
def test_project_conversation_uses_conversation_root(env, monkeypatch, case):
    import inspect
    if "monkeypatch" in inspect.signature(case).parameters:
        rel = _run(case(env, monkeypatch))
    else:
        rel = _run(case(env))
    assert (env.conv_root / rel).is_file()
    assert not env.projects.exists()
    for event in env.events:
        assert event["scope"] == "conversation"
        assert event["conversation_id"] == CID


def test_io_attachment_missing_in_project_conversation_hints_copy(env):
    from chat.action_request_types._io_attachments import resolve_workspace_file
    with pytest.raises(RuntimeError) as excinfo:
        _run(resolve_workspace_file(CID, PID, "shared/report.pdf"))
    assert "copy_project_file" in str(excinfo.value)
    with pytest.raises(RuntimeError) as excinfo:
        _run(resolve_workspace_file(CID, None, "shared/report.pdf"))
    assert "copy_project_file" not in str(excinfo.value)


def test_gmail_attachment_missing_in_project_conversation_hints_copy(env, monkeypatch):
    import api.gmail.draft_endpoints as draft_endpoints
    from api.gmail.models import DraftAttachment
    from fastapi import HTTPException

    async def fake_owned(user_id, conversation_id):
        return {"id": conversation_id, "project_id": PID}

    monkeypatch.setattr(draft_endpoints, "require_owned_conversation", fake_owned)
    with pytest.raises(HTTPException) as excinfo:
        _run(draft_endpoints._resolve_request_attachments(
            USER, [DraftAttachment(type="workspace", workspace_path="x.pdf")], CID,
        ))
    assert excinfo.value.status_code == 404
    assert "copy_project_file" in excinfo.value.detail


def test_add_doc_image_missing_in_project_conversation_hints_copy(env):
    from chat.docs.service import Caller, DocError, _load_workspace_image

    caller = Caller(
        user=USER, conversation_id=CID, project_id=PID,
        is_public=False, run_kind="top_level",
    )
    with pytest.raises(DocError) as excinfo:
        _run(_load_workspace_image(caller, "fig.png"))
    assert "copy_project_file" in str(excinfo.value)
