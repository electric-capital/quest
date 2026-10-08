"""Tests for the upload_to_drive action_request handler.

Covers ``UploadToDriveHandler.validate_params`` (unknown-key rejection,
missing/empty path, absolute/`..` rejection, filename normalization,
folder_id passthrough) and ``UploadToDriveHandler.execute`` (reads a
workspace file and uploads it via a mocked ``make_authenticated_request``).
"""

import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


def _handler():
    from chat.action_request_types.upload_to_drive import UploadToDriveHandler
    return UploadToDriveHandler()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_handler_metadata():
    from db.models import ActionRequestType

    handler = _handler()
    assert handler.type_name == ActionRequestType.UPLOAD_TO_DRIVE
    assert handler.display_name == "Upload to Drive"
    assert handler.approve_label == "Upload"


# ---------------------------------------------------------------------------
# validate_params
# ---------------------------------------------------------------------------


def test_validate_params_rejects_unknown_field():
    handler = _handler()
    with pytest.raises(ValueError, match="Unknown parameter for upload_to_drive"):
        handler.validate_params({"path": "report.pdf", "mimeType": "application/pdf"})


def test_validate_params_rejects_missing_path():
    handler = _handler()
    with pytest.raises(ValueError, match="path"):
        handler.validate_params({})


def test_validate_params_rejects_empty_path():
    handler = _handler()
    with pytest.raises(ValueError, match="non-empty"):
        handler.validate_params({"path": "   "})


def test_validate_params_rejects_absolute_path():
    handler = _handler()
    with pytest.raises(ValueError, match="absolute"):
        handler.validate_params({"path": "/etc/passwd"})


def test_validate_params_rejects_traversal():
    handler = _handler()
    with pytest.raises(ValueError, match=r"\.\."):
        handler.validate_params({"path": "../secret.txt"})


def test_validate_params_strips_path():
    handler = _handler()
    out = handler.validate_params({"path": "  report.pdf  "})
    assert out == {"path": "report.pdf"}


def test_validate_params_normalizes_filename():
    handler = _handler()
    out = handler.validate_params({"path": "report.pdf", "filename": "  My Report.pdf  "})
    assert out["filename"] == "My Report.pdf"


def test_validate_params_drops_empty_filename():
    handler = _handler()
    # A filename that sanitizes to empty (only whitespace / dots) is dropped
    # and the basename is used at execute() time instead.
    out = handler.validate_params({"path": "report.pdf", "filename": "   "})
    assert "filename" not in out


def test_validate_params_drops_leading_dots_in_filename():
    handler = _handler()
    out = handler.validate_params({"path": "report.pdf", "filename": ".hidden.pdf"})
    assert out["filename"] == "hidden.pdf"


def test_validate_params_folder_id_passthrough():
    handler = _handler()
    out = handler.validate_params({"path": "report.pdf", "folder_id": "  ABC123  "})
    assert out["folder_id"] == "ABC123"


def test_validate_params_rejects_empty_folder_id():
    handler = _handler()
    with pytest.raises(ValueError, match="folder_id"):
        handler.validate_params({"path": "report.pdf", "folder_id": "   "})


def test_validate_params_rejects_non_string_folder_id():
    handler = _handler()
    with pytest.raises(ValueError, match="folder_id"):
        handler.validate_params({"path": "report.pdf", "folder_id": 42})


def test_validate_params_new_folder_name_trimmed():
    handler = _handler()
    out = handler.validate_params({"path": "report.pdf", "new_folder_name": "  Q1 Reports  "})
    assert out["new_folder_name"] == "Q1 Reports"


def test_validate_params_rejects_empty_new_folder_name():
    handler = _handler()
    with pytest.raises(ValueError, match="new_folder_name"):
        handler.validate_params({"path": "report.pdf", "new_folder_name": "   "})


def test_validate_params_rejects_non_string_new_folder_name():
    handler = _handler()
    with pytest.raises(ValueError, match="new_folder_name"):
        handler.validate_params({"path": "report.pdf", "new_folder_name": 42})


def test_validate_params_rejects_folder_id_with_new_folder_name():
    handler = _handler()
    with pytest.raises(ValueError, match="mutually exclusive"):
        handler.validate_params({
            "path": "report.pdf",
            "folder_id": "ABC",
            "new_folder_name": "Q1 Reports",
        })


def test_validate_params_new_folder_parent_id_passthrough():
    handler = _handler()
    out = handler.validate_params({
        "path": "report.pdf",
        "new_folder_name": "Q1 Reports",
        "new_folder_parent_id": "  PARENT1  ",
    })
    assert out["new_folder_parent_id"] == "PARENT1"


def test_validate_params_rejects_parent_id_without_new_folder_name():
    handler = _handler()
    with pytest.raises(ValueError, match="new_folder_name"):
        handler.validate_params({"path": "report.pdf", "new_folder_parent_id": "PARENT1"})


def test_validate_params_rejects_empty_new_folder_parent_id():
    handler = _handler()
    with pytest.raises(ValueError, match="new_folder_parent_id"):
        handler.validate_params({
            "path": "report.pdf",
            "new_folder_name": "Q1 Reports",
            "new_folder_parent_id": "   ",
        })


def test_validate_params_rejects_non_string_new_folder_parent_id():
    handler = _handler()
    with pytest.raises(ValueError, match="new_folder_parent_id"):
        handler.validate_params({
            "path": "report.pdf",
            "new_folder_name": "Q1 Reports",
            "new_folder_parent_id": 42,
        })


# ---------------------------------------------------------------------------
# render_preview
# ---------------------------------------------------------------------------


def test_render_preview_default_destination_root():
    handler = _handler()
    out = _run(handler.render_preview({"path": "out/report.pdf"}))
    assert {"key": "File", "value": "report.pdf"} in out
    assert {"key": "Source", "value": "out/report.pdf"} in out
    assert {"key": "Destination", "value": "My Drive (root)"} in out


def test_render_preview_uses_filename_override_and_folder_name():
    handler = _handler()
    out = _run(handler.render_preview({
        "path": "out/report.pdf",
        "filename": "Q1.pdf",
        "folder_id": "ABC",
        "folder_name": "Quarterly Reports",
    }))
    assert {"key": "File", "value": "Q1.pdf"} in out
    assert {"key": "Destination", "value": "Quarterly Reports"} in out


def test_render_preview_falls_back_to_folder_id():
    handler = _handler()
    # No folder_name injected and no user -> show the raw id.
    out = _run(handler.render_preview({"path": "report.pdf", "folder_id": "ABC"}))
    assert {"key": "Destination", "value": "ABC"} in out


def test_render_preview_new_folder_in_root():
    handler = _handler()
    out = _run(handler.render_preview({
        "path": "report.pdf",
        "new_folder_name": "Q1 Reports",
    }))
    assert {"key": "Destination", "value": 'New folder "Q1 Reports" in My Drive (root)'} in out


def test_render_preview_new_folder_with_injected_parent_name():
    handler = _handler()
    out = _run(handler.render_preview({
        "path": "report.pdf",
        "new_folder_name": "Q1 Reports",
        "new_folder_parent_id": "PARENT1",
        "new_folder_parent_name": "Quarterly",
    }))
    assert {"key": "Destination", "value": 'New folder "Q1 Reports" in Quarterly'} in out


def test_render_preview_new_folder_falls_back_to_parent_id():
    handler = _handler()
    # No new_folder_parent_name injected and no user -> show the raw id.
    out = _run(handler.render_preview({
        "path": "report.pdf",
        "new_folder_name": "Q1 Reports",
        "new_folder_parent_id": "PARENT1",
    }))
    assert {"key": "Destination", "value": 'New folder "Q1 Reports" in PARENT1'} in out


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def _user_with_drive_scope():
    return {
        "id": 1,
        "google_services_oauth": {
            "access_token": "tok",
            "scopes": ["https://www.googleapis.com/auth/drive.file"],
        },
    }


def test_execute_rejects_when_not_connected():
    handler = _handler()
    with pytest.raises(RuntimeError, match="not connected"):
        _run(handler.execute({"path": "report.pdf"}, {"id": 1, "google_services_oauth": {}},
                             conversation_id="c1"))


def test_execute_rejects_when_missing_scope():
    handler = _handler()
    user = {
        "id": 1,
        "google_services_oauth": {"access_token": "tok", "scopes": []},
    }
    with pytest.raises(RuntimeError, match="reconnect"):
        _run(handler.execute({"path": "report.pdf"}, user, conversation_id="c1"))


def test_execute_uploads_workspace_file(tmp_path, monkeypatch):
    handler = _handler()

    # Set up a fake workspace dir with a file.
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "report.pdf").write_bytes(b"%PDF-1.4 fake bytes")

    async def _fake_workspace_dir(conversation_id, project_id=None):
        return workspace_dir

    monkeypatch.setattr(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        _fake_workspace_dir,
    )

    captured: dict = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "FILE123", "name": "report.pdf"}

    async def _fake_request(client, user, method, url, **kwargs):
        captured["url"] = url
        captured["content"] = kwargs.get("content")
        captured["headers"] = kwargs.get("headers")
        return _FakeResponse()

    monkeypatch.setattr(
        "chat.action_request_types.upload_to_drive.make_authenticated_request",
        _fake_request,
    )

    result = _run(handler.execute(
        {"path": "report.pdf", "folder_id": "FOLDER1"},
        _user_with_drive_scope(),
        conversation_id="c1",
    ))

    assert result["success"] is True
    assert result["file_id"] == "FILE123"
    assert result["url"] == "https://drive.google.com/file/d/FILE123/view"
    # Multipart body must carry the raw file bytes and the parents folder.
    assert b"%PDF-1.4 fake bytes" in captured["content"]
    assert b'"parents": ["FOLDER1"]' in captured["content"]
    assert "uploadType=multipart" in captured["url"]
    assert "supportsAllDrives=true" in captured["url"]


def test_execute_uses_filename_override(tmp_path, monkeypatch):
    handler = _handler()

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "report.pdf").write_bytes(b"data")

    async def _fake_workspace_dir(conversation_id, project_id=None):
        return workspace_dir

    monkeypatch.setattr(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        _fake_workspace_dir,
    )

    captured: dict = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "X", "name": "Custom.pdf"}

    async def _fake_request(client, user, method, url, **kwargs):
        captured["content"] = kwargs.get("content")
        return _FakeResponse()

    monkeypatch.setattr(
        "chat.action_request_types.upload_to_drive.make_authenticated_request",
        _fake_request,
    )

    result = _run(handler.execute(
        {"path": "report.pdf", "filename": "Custom.pdf"},
        _user_with_drive_scope(),
        conversation_id="c1",
    ))
    assert result["name"] == "Custom.pdf"
    assert b'"name": "Custom.pdf"' in captured["content"]


def test_execute_missing_file_raises(tmp_path, monkeypatch):
    handler = _handler()

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    async def _fake_workspace_dir(conversation_id, project_id=None):
        return workspace_dir

    monkeypatch.setattr(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        _fake_workspace_dir,
    )

    with pytest.raises(RuntimeError, match="not found"):
        _run(handler.execute(
            {"path": "missing.pdf"},
            _user_with_drive_scope(),
            conversation_id="c1",
        ))


def test_execute_drive_error_raises(tmp_path, monkeypatch):
    handler = _handler()

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "report.pdf").write_bytes(b"data")

    async def _fake_workspace_dir(conversation_id, project_id=None):
        return workspace_dir

    monkeypatch.setattr(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        _fake_workspace_dir,
    )

    class _FakeResponse:
        status_code = 404

        def json(self):
            return {"error": {"message": "File not found: FOLDER1"}}

        text = ""

    async def _fake_request(client, user, method, url, **kwargs):
        return _FakeResponse()

    monkeypatch.setattr(
        "chat.action_request_types.upload_to_drive.make_authenticated_request",
        _fake_request,
    )

    with pytest.raises(RuntimeError, match="File not found: FOLDER1"):
        _run(handler.execute(
            {"path": "report.pdf", "folder_id": "FOLDER1"},
            _user_with_drive_scope(),
            conversation_id="c1",
        ))


def test_execute_creates_folder_then_uploads(tmp_path, monkeypatch):
    handler = _handler()

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "report.pdf").write_bytes(b"data")

    async def _fake_workspace_dir(conversation_id, project_id=None):
        return workspace_dir

    monkeypatch.setattr(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        _fake_workspace_dir,
    )

    folder_calls: list = []

    async def _fake_create_folder(user, name, parent_folder_id=None):
        folder_calls.append((name, parent_folder_id))
        return {"id": "NEWFOLDER1", "name": name}

    monkeypatch.setattr(
        "chat.action_request_types._drive.create_folder",
        _fake_create_folder,
    )

    captured: dict = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "FILE123", "name": "report.pdf"}

    async def _fake_request(client, user, method, url, **kwargs):
        captured["content"] = kwargs.get("content")
        return _FakeResponse()

    monkeypatch.setattr(
        "chat.action_request_types.upload_to_drive.make_authenticated_request",
        _fake_request,
    )

    result = _run(handler.execute(
        {
            "path": "report.pdf",
            "new_folder_name": "Q1 Reports",
            "new_folder_parent_id": "PARENT1",
        },
        _user_with_drive_scope(),
        conversation_id="c1",
    ))

    assert folder_calls == [("Q1 Reports", "PARENT1")]
    # The upload metadata parents must be the freshly created folder id.
    assert b'"parents": ["NEWFOLDER1"]' in captured["content"]
    assert result["success"] is True
    assert result["file_id"] == "FILE123"
    assert result["folder_id"] == "NEWFOLDER1"
    assert result["folder_name"] == "Q1 Reports"
    assert result["folder_url"] == "https://drive.google.com/drive/folders/NEWFOLDER1"


def test_execute_folder_creation_failure_skips_upload(tmp_path, monkeypatch):
    handler = _handler()

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "report.pdf").write_bytes(b"data")

    async def _fake_workspace_dir(conversation_id, project_id=None):
        return workspace_dir

    monkeypatch.setattr(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        _fake_workspace_dir,
    )

    async def _fake_create_folder(user, name, parent_folder_id=None):
        raise RuntimeError("Google Drive API error: File not found: PARENT1")

    monkeypatch.setattr(
        "chat.action_request_types._drive.create_folder",
        _fake_create_folder,
    )

    upload_calls: list = []

    async def _fake_request(client, user, method, url, **kwargs):
        upload_calls.append(url)
        raise AssertionError("upload must not run when folder creation fails")

    monkeypatch.setattr(
        "chat.action_request_types.upload_to_drive.make_authenticated_request",
        _fake_request,
    )

    with pytest.raises(RuntimeError, match="File not found: PARENT1"):
        _run(handler.execute(
            {"path": "report.pdf", "new_folder_name": "Q1 Reports",
             "new_folder_parent_id": "PARENT1"},
            _user_with_drive_scope(),
            conversation_id="c1",
        ))
    assert upload_calls == []


def test_execute_upload_failure_after_folder_creation_mentions_folder_id(tmp_path, monkeypatch):
    handler = _handler()

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "report.pdf").write_bytes(b"data")

    async def _fake_workspace_dir(conversation_id, project_id=None):
        return workspace_dir

    monkeypatch.setattr(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        _fake_workspace_dir,
    )

    async def _fake_create_folder(user, name, parent_folder_id=None):
        return {"id": "NEWFOLDER1", "name": name}

    monkeypatch.setattr(
        "chat.action_request_types._drive.create_folder",
        _fake_create_folder,
    )

    class _FakeResponse:
        status_code = 500

        def json(self):
            return {"error": {"message": "Backend Error"}}

        text = ""

    async def _fake_request(client, user, method, url, **kwargs):
        return _FakeResponse()

    monkeypatch.setattr(
        "chat.action_request_types.upload_to_drive.make_authenticated_request",
        _fake_request,
    )

    # A retry would create a duplicate folder; the error must carry the
    # created folder id as the recovery path.
    with pytest.raises(RuntimeError, match="NEWFOLDER1"):
        _run(handler.execute(
            {"path": "report.pdf", "new_folder_name": "Q1 Reports"},
            _user_with_drive_scope(),
            conversation_id="c1",
        ))


# ---------------------------------------------------------------------------
# validate_params -- multi-file `files` shape
# ---------------------------------------------------------------------------


def test_validate_params_files_normalizes_entries():
    handler = _handler()
    out = handler.validate_params({
        "files": [
            {"path": "  report.pdf  ", "filename": "  My Report.pdf  "},
            {"path": "out/data.csv"},
        ],
    })
    assert out == {
        "files": [
            {"path": "report.pdf", "filename": "My Report.pdf"},
            {"path": "out/data.csv"},
        ],
    }


def test_validate_params_files_rejects_path_alongside():
    handler = _handler()
    with pytest.raises(ValueError, match="mutually exclusive"):
        handler.validate_params({
            "files": [{"path": "a.pdf"}],
            "path": "b.pdf",
        })


def test_validate_params_files_rejects_empty_list():
    handler = _handler()
    with pytest.raises(ValueError, match="non-empty list"):
        handler.validate_params({"files": []})


def test_validate_params_files_rejects_over_cap():
    handler = _handler()
    with pytest.raises(ValueError, match="at most 10"):
        handler.validate_params({
            "files": [{"path": f"f{i}.txt"} for i in range(11)],
        })


def test_validate_params_files_rejects_duplicate_path():
    handler = _handler()
    with pytest.raises(ValueError, match="duplicates"):
        handler.validate_params({
            "files": [{"path": "a.pdf"}, {"path": "a.pdf"}],
        })


def test_validate_params_files_rejects_absolute_entry_path():
    handler = _handler()
    with pytest.raises(ValueError, match=r"files\[1\]\.path.*absolute"):
        handler.validate_params({
            "files": [{"path": "ok.pdf"}, {"path": "/etc/passwd"}],
        })


def test_validate_params_files_rejects_traversal_entry_path():
    handler = _handler()
    with pytest.raises(ValueError, match=r"\.\."):
        handler.validate_params({"files": [{"path": "../secret.txt"}]})


def test_validate_params_files_rejects_unknown_inner_key():
    handler = _handler()
    with pytest.raises(ValueError, match="Unknown parameter"):
        handler.validate_params({
            "files": [{"path": "a.pdf", "mimeType": "application/pdf"}],
        })


def test_validate_params_files_drops_empty_filename():
    handler = _handler()
    out = handler.validate_params({
        "files": [{"path": "a.pdf", "filename": ".."}],
    })
    assert out == {"files": [{"path": "a.pdf"}]}


def test_validate_params_files_with_folder_params():
    handler = _handler()
    out = handler.validate_params({
        "files": [{"path": "a.pdf"}],
        "new_folder_name": "Q1",
        "new_folder_parent_id": "PARENT1",
    })
    assert out["files"] == [{"path": "a.pdf"}]
    assert out["new_folder_name"] == "Q1"
    assert out["new_folder_parent_id"] == "PARENT1"


# ---------------------------------------------------------------------------
# render_preview -- multi-file
# ---------------------------------------------------------------------------


def test_render_preview_single_entry_files_uses_classic_rows():
    handler = _handler()
    out = _run(handler.render_preview({
        "files": [{"path": "out/report.pdf", "filename": "Q1.pdf"}],
    }))
    assert {"key": "File", "value": "Q1.pdf"} in out
    assert {"key": "Source", "value": "out/report.pdf"} in out
    assert {"key": "Destination", "value": "My Drive (root)"} in out


def test_render_preview_multi_file_rows():
    handler = _handler()
    out = _run(handler.render_preview({
        "files": [
            {"path": "out/report.pdf", "filename": "Q1.pdf"},
            {"path": "out/data.csv"},
        ],
        "folder_id": "ABC",
        "folder_name": "Quarterly Reports",
    }))
    assert {"key": "Files", "value": "2 files"} in out
    assert {"key": "File 1", "value": "out/report.pdf → Q1.pdf"} in out
    assert {"key": "File 2", "value": "out/data.csv"} in out
    assert {"key": "Destination", "value": "Quarterly Reports"} in out


# ---------------------------------------------------------------------------
# execute -- multi-file
# ---------------------------------------------------------------------------


def test_execute_uploads_multiple_files(tmp_path, monkeypatch):
    handler = _handler()

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "report.pdf").write_bytes(b"%PDF-1.4 fake bytes")
    (workspace_dir / "data.csv").write_bytes(b"a,b\n1,2\n")

    async def _fake_workspace_dir(conversation_id, project_id=None):
        return workspace_dir

    monkeypatch.setattr(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        _fake_workspace_dir,
    )

    calls: list = []

    class _FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    async def _fake_request(client, user, method, url, **kwargs):
        idx = len(calls)
        calls.append(kwargs.get("content"))
        return _FakeResponse({"id": f"FILE{idx}", "name": f"name{idx}"})

    monkeypatch.setattr(
        "chat.action_request_types.upload_to_drive.make_authenticated_request",
        _fake_request,
    )

    result = _run(handler.execute(
        {
            "files": [
                {"path": "report.pdf", "filename": "Q1.pdf"},
                {"path": "data.csv"},
            ],
            "folder_id": "FOLDER1",
        },
        _user_with_drive_scope(),
        conversation_id="c1",
    ))

    assert result["success"] is True
    assert result["count"] == 2
    assert result["files"] == [
        {"file_id": "FILE0", "name": "name0",
         "url": "https://drive.google.com/file/d/FILE0/view"},
        {"file_id": "FILE1", "name": "name1",
         "url": "https://drive.google.com/file/d/FILE1/view"},
    ]
    # Two sequential uploads, each with the raw bytes and the shared folder.
    assert len(calls) == 2
    assert b"%PDF-1.4 fake bytes" in calls[0]
    assert b'"name": "Q1.pdf"' in calls[0]
    assert b"a,b" in calls[1]
    assert b'"parents": ["FOLDER1"]' in calls[0]
    assert b'"parents": ["FOLDER1"]' in calls[1]


def test_execute_single_entry_files_returns_list_shape(tmp_path, monkeypatch):
    handler = _handler()

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "report.pdf").write_bytes(b"data")

    async def _fake_workspace_dir(conversation_id, project_id=None):
        return workspace_dir

    monkeypatch.setattr(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        _fake_workspace_dir,
    )

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "FILE123", "name": "report.pdf"}

    async def _fake_request(client, user, method, url, **kwargs):
        return _FakeResponse()

    monkeypatch.setattr(
        "chat.action_request_types.upload_to_drive.make_authenticated_request",
        _fake_request,
    )

    result = _run(handler.execute(
        {"files": [{"path": "report.pdf"}]},
        _user_with_drive_scope(),
        conversation_id="c1",
    ))
    assert result["count"] == 1
    assert result["files"][0]["file_id"] == "FILE123"
    assert "file_id" not in result  # flat legacy keys only for `path` shape


def test_execute_missing_file_fails_batch_before_any_upload(tmp_path, monkeypatch):
    handler = _handler()

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "report.pdf").write_bytes(b"data")

    async def _fake_workspace_dir(conversation_id, project_id=None):
        return workspace_dir

    monkeypatch.setattr(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        _fake_workspace_dir,
    )

    upload_calls: list = []

    async def _fake_request(client, user, method, url, **kwargs):
        upload_calls.append(url)
        raise AssertionError("no upload may run when pre-flight fails")

    monkeypatch.setattr(
        "chat.action_request_types.upload_to_drive.make_authenticated_request",
        _fake_request,
    )

    folder_calls: list = []

    async def _fake_create_folder(user, name, parent_folder_id=None):
        folder_calls.append(name)
        raise AssertionError("no folder may be created when pre-flight fails")

    monkeypatch.setattr(
        "chat.action_request_types._drive.create_folder",
        _fake_create_folder,
    )

    with pytest.raises(RuntimeError, match="not found"):
        _run(handler.execute(
            {
                "files": [{"path": "report.pdf"}, {"path": "missing.pdf"}],
                "new_folder_name": "Q1 Reports",
            },
            _user_with_drive_scope(),
            conversation_id="c1",
        ))
    assert upload_calls == []
    assert folder_calls == []


def test_execute_mid_batch_failure_lists_uploaded_files(tmp_path, monkeypatch):
    handler = _handler()

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "a.pdf").write_bytes(b"data-a")
    (workspace_dir / "b.pdf").write_bytes(b"data-b")

    async def _fake_workspace_dir(conversation_id, project_id=None):
        return workspace_dir

    monkeypatch.setattr(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        _fake_workspace_dir,
    )

    class _OkResponse:
        status_code = 200

        def json(self):
            return {"id": "FILE-A", "name": "a.pdf"}

    class _ErrResponse:
        status_code = 500
        text = ""

        def json(self):
            return {"error": {"message": "Backend Error"}}

    responses = [_OkResponse(), _ErrResponse()]

    async def _fake_request(client, user, method, url, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(
        "chat.action_request_types.upload_to_drive.make_authenticated_request",
        _fake_request,
    )

    with pytest.raises(RuntimeError) as excinfo:
        _run(handler.execute(
            {"files": [{"path": "a.pdf"}, {"path": "b.pdf"}]},
            _user_with_drive_scope(),
            conversation_id="c1",
        ))
    message = str(excinfo.value)
    assert "1 of 2 files were already uploaded" in message
    assert "a.pdf (FILE-A)" in message
    assert "only the remaining files" in message


def test_execute_mid_batch_failure_mentions_created_folder(tmp_path, monkeypatch):
    handler = _handler()

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "a.pdf").write_bytes(b"data-a")
    (workspace_dir / "b.pdf").write_bytes(b"data-b")

    async def _fake_workspace_dir(conversation_id, project_id=None):
        return workspace_dir

    monkeypatch.setattr(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        _fake_workspace_dir,
    )

    async def _fake_create_folder(user, name, parent_folder_id=None):
        return {"id": "NEWFOLDER1", "name": name}

    monkeypatch.setattr(
        "chat.action_request_types._drive.create_folder",
        _fake_create_folder,
    )

    class _OkResponse:
        status_code = 200

        def json(self):
            return {"id": "FILE-A", "name": "a.pdf"}

    class _ErrResponse:
        status_code = 500
        text = ""

        def json(self):
            return {"error": {"message": "Backend Error"}}

    responses = [_OkResponse(), _ErrResponse()]

    async def _fake_request(client, user, method, url, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(
        "chat.action_request_types.upload_to_drive.make_authenticated_request",
        _fake_request,
    )

    with pytest.raises(RuntimeError) as excinfo:
        _run(handler.execute(
            {
                "files": [{"path": "a.pdf"}, {"path": "b.pdf"}],
                "new_folder_name": "Q1 Reports",
            },
            _user_with_drive_scope(),
            conversation_id="c1",
        ))
    message = str(excinfo.value)
    assert "a.pdf (FILE-A)" in message
    assert "NEWFOLDER1" in message
    assert "folder_id=NEWFOLDER1" in message
