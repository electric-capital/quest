"""Workspace symlink hardening tests.

A symlink planted in a host-backed workspace (only ever possible from the
script sandbox's read-write mount) used to let host-side file operations
read or overwrite arbitrary host files: folder zip downloads and workspace
duplication dereferenced links, and uploads truncating-wrote through a
symlink leaf. The primary fix is the sandbox seccomp profile that denies
symlink creation at the syscall level (pinned in
``tests/test_public_projects.py::TestBuildScriptPodmanCmd``); this module
covers the profile generation itself, the startup scrub for pre-existing
links, and the host-side defense in depth in every workspace consumer.
"""

import asyncio
import errno
import json
import zipfile
from pathlib import Path

import pytest

import chat.gemini_api.sandbox_seccomp as sandbox_seccomp
import chat.storage as storage_mod
from chat.file_storage import (
    create_folder_zip,
    save_uploaded_file,
    save_uploaded_file_with_path,
)
from chat.storage import ChatStorage
from chat.workspace_symlinks import remove_symlinks_under, scrub_workspace_symlinks
from config import paths


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Seccomp profile generation
# ---------------------------------------------------------------------------


class TestSandboxSeccompProfile:
    @pytest.fixture(autouse=True)
    def _isolated_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(sandbox_seccomp, "_generated", None)

    def test_generated_profile_denies_only_symlink_syscalls(self):
        profile_path = sandbox_seccomp.get_sandbox_seccomp_profile_path()
        profile = json.loads(profile_path.read_text())

        # The base profile's shape survives (this is what keeps the rest of
        # the default confinement, e.g. the io_uring denial, intact).
        assert profile["defaultAction"]
        allowed = {
            name
            for group in profile["syscalls"]
            if group["action"] == "SCMP_ACT_ALLOW"
            for name in group["names"]
        }
        # Ordinary syscalls stay allowed; only symlink creation is pulled.
        assert "openat" in allowed
        assert "linkat" in allowed
        assert "symlink" not in allowed
        assert "symlinkat" not in allowed

        deny = [
            g for g in profile["syscalls"]
            if set(g["names"]) & {"symlink", "symlinkat"}
        ]
        assert len(deny) == 1
        assert deny[0]["action"] == "SCMP_ACT_ERRNO"
        assert deny[0]["errnoRet"] == errno.EPERM

    def test_vendored_fallback_used_when_host_profiles_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            sandbox_seccomp,
            "_DEFAULT_PROFILE_PATHS",
            (tmp_path / "nope-etc.json", tmp_path / "nope-usr.json"),
        )
        profile_path = sandbox_seccomp.get_sandbox_seccomp_profile_path()
        profile = json.loads(profile_path.read_text())
        assert any(
            g["action"] == "SCMP_ACT_ERRNO" and g["names"] == ["symlink", "symlinkat"]
            for g in profile["syscalls"]
        )

    def test_no_usable_source_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            sandbox_seccomp, "_DEFAULT_PROFILE_PATHS", (tmp_path / "nope.json",),
        )
        monkeypatch.setattr(
            sandbox_seccomp, "FALLBACK_PROFILE", tmp_path / "also-nope.json",
        )
        with pytest.raises(RuntimeError):
            sandbox_seccomp.get_sandbox_seccomp_profile_path()

    def test_generation_is_cached_then_self_heals_deletion(self):
        first = sandbox_seccomp.get_sandbox_seccomp_profile_path()
        mtime = first.stat().st_mtime_ns
        assert sandbox_seccomp.get_sandbox_seccomp_profile_path() == first
        assert first.stat().st_mtime_ns == mtime  # cached, not rewritten
        first.unlink()
        assert sandbox_seccomp.get_sandbox_seccomp_profile_path().is_file()


# ---------------------------------------------------------------------------
# Startup scrub
# ---------------------------------------------------------------------------


class TestWorkspaceSymlinkScrub:
    def test_scrub_removes_links_at_any_depth_and_keeps_files(self, tmp_path, monkeypatch):
        chats = tmp_path / "chats"
        projects = tmp_path / "projects"
        monkeypatch.setattr(paths, "CHATS_DIR", chats)
        monkeypatch.setattr(paths, "PROJECTS_DIR", projects)

        outside = tmp_path / "host-secret.txt"
        outside.write_text("secret")
        outside_dir = tmp_path / "host-dir"
        (outside_dir / "inner").mkdir(parents=True)

        ws = chats / "c1" / "workspace"
        (ws / "sub").mkdir(parents=True)
        (ws / "keep.txt").write_text("keep")
        (ws / "leak.txt").symlink_to(outside)
        (ws / "sub" / "nested-leak.txt").symlink_to(outside)
        (ws / "dir-leak").symlink_to(outside_dir)
        (ws / "broken").symlink_to(tmp_path / "does-not-exist")

        pws = projects / "p1" / "workspace" / "workspace"
        pws.mkdir(parents=True)
        (pws / "proj-leak.txt").symlink_to(outside)

        assert scrub_workspace_symlinks() == 5
        assert (ws / "keep.txt").read_text() == "keep"
        assert not (ws / "leak.txt").is_symlink()
        assert not (ws / "sub" / "nested-leak.txt").exists()
        assert not (ws / "dir-leak").is_symlink()
        assert not (pws / "proj-leak.txt").exists()
        # Symlinked dirs are removed, never descended into: targets intact.
        assert outside.read_text() == "secret"
        assert (outside_dir / "inner").is_dir()

        # Idempotent, and missing roots are a no-op.
        assert scrub_workspace_symlinks() == 0

    def test_scrub_covers_project_conversation_and_project_roots(
        self, tmp_path, monkeypatch,
    ):
        # A project conversation has its own conversation workspace under
        # CHATS_DIR, next to the project's shared root under PROJECTS_DIR;
        # the scrub must reach both through the resolvers' layout.
        chats = tmp_path / "chats"
        projects = tmp_path / "projects"
        monkeypatch.setattr(paths, "CHATS_DIR", chats)
        monkeypatch.setattr(paths, "PROJECTS_DIR", projects)
        monkeypatch.setattr(storage_mod, "CHATS_DIR", chats)
        monkeypatch.setattr(storage_mod, "PROJECTS_DIR", projects)

        outside = tmp_path / "host-secret.txt"
        outside.write_text("secret")

        conv_root = ChatStorage.get_conversation_workspace_root("proj-conv")
        (conv_root / "deep").mkdir(parents=True)
        (conv_root / "chat-leak.txt").symlink_to(outside)
        (conv_root / "deep" / "nested-leak.txt").symlink_to(outside)
        (conv_root / "keep.txt").write_text("keep")

        project_root = ChatStorage.create_project_workspace("p1")
        (project_root / "proj-leak.txt").symlink_to(outside)
        (project_root / "shared.txt").write_text("shared")

        assert scrub_workspace_symlinks() == 3
        assert not list(p for p in chats.rglob("*") if p.is_symlink())
        assert not list(p for p in projects.rglob("*") if p.is_symlink())
        assert (conv_root / "keep.txt").read_text() == "keep"
        assert (project_root / "shared.txt").read_text() == "shared"
        assert outside.read_text() == "secret"

    def test_deep_tree_does_not_recurse(self, tmp_path):
        deep = tmp_path
        for i in range(300):
            deep = deep / f"d{i}"
        deep.mkdir(parents=True)
        (deep / "leak").symlink_to(tmp_path)
        assert remove_symlinks_under(tmp_path) == 1


# ---------------------------------------------------------------------------
# Folder zip download (defense in depth)
# ---------------------------------------------------------------------------


class TestFolderZipRejectsSymlinks:
    def test_zip_refuses_folder_with_symlink_descendant(self, tmp_path):
        base = tmp_path / "conv"
        loot = base / "workspace" / "loot" / "sub"
        loot.mkdir(parents=True)
        secret = tmp_path / "host-secret.txt"
        secret.write_text("server-side secret outside workspace")
        (loot / "linked-secret.txt").symlink_to(secret)

        with pytest.raises(ValueError, match="symbolic links"):
            create_folder_zip(base / "workspace", "loot")

    def test_zip_of_clean_folder_still_works(self, tmp_path):
        base = tmp_path / "conv"
        folder = base / "workspace" / "reports"
        (folder / "empty").mkdir(parents=True)
        (folder / "a.txt").write_text("hello")

        zip_path, folder_name = create_folder_zip(base / "workspace", "reports")
        try:
            assert folder_name == "reports"
            with zipfile.ZipFile(zip_path) as zf:
                assert zf.read("reports/a.txt") == b"hello"
        finally:
            zip_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Workspace duplication (defense in depth)
# ---------------------------------------------------------------------------


class TestCopyWorkspaceSkipsSymlinks:
    def test_duplicate_workspace_never_dereferences_links(self, tmp_path, monkeypatch):
        chats = tmp_path / "chats"
        monkeypatch.setattr(storage_mod, "CHATS_DIR", chats)

        secret = tmp_path / "host-secret.txt"
        secret.write_text("server-side secret outside workspace")

        src_ws = chats / "src-conv" / "workspace"
        (src_ws / "loot").mkdir(parents=True)
        (src_ws / ".responses").mkdir()
        (src_ws / ".responses" / "blob.json").write_text("{}")
        (src_ws / "keep.txt").write_text("keep")
        (src_ws / "leak.txt").symlink_to(secret)
        (src_ws / "loot" / "nested-leak.txt").symlink_to(secret)

        _run(ChatStorage.copy_workspace_files(
            ChatStorage.get_conversation_workspace_root("src-conv"),
            ChatStorage.get_conversation_workspace_root("dest-conv"),
        ))

        dest_ws = chats / "dest-conv" / "workspace"
        assert (dest_ws / "keep.txt").read_text() == "keep"
        assert not (dest_ws / "leak.txt").exists()
        assert not (dest_ws / "loot" / "nested-leak.txt").exists()
        # The pre-existing .responses skip still holds alongside the new rule.
        assert not (dest_ws / ".responses").exists()


# ---------------------------------------------------------------------------
# Uploads (defense in depth)
# ---------------------------------------------------------------------------


class TestUploadRefusesSymlinkLeaf:
    def test_save_uploaded_file_rejects_symlink_leaf(self, tmp_path):
        base = tmp_path / "conv"
        ws = base / "workspace"
        ws.mkdir(parents=True)
        target = tmp_path / "host-target.txt"
        target.write_text("original service-owned content")
        (ws / "overwrite-me.txt").symlink_to(target)

        with pytest.raises(ValueError, match="symbolic link"):
            _run(save_uploaded_file(base / "workspace", "overwrite-me.txt", b"attacker bytes"))
        assert target.read_text() == "original service-owned content"

    def test_save_uploaded_file_with_path_rejects_symlink_leaf(self, tmp_path):
        base = tmp_path / "conv"
        sub = base / "workspace" / "folder"
        sub.mkdir(parents=True)
        target = tmp_path / "host-target.txt"
        target.write_text("original service-owned content")
        (sub / "overwrite-me.txt").symlink_to(target)

        with pytest.raises(ValueError, match="symbolic link"):
            _run(save_uploaded_file_with_path(
                base / "workspace", "folder/overwrite-me.txt", b"attacker bytes",
            ))
        assert target.read_text() == "original service-owned content"

    def test_normal_uploads_and_regular_overwrites_still_work(self, tmp_path):
        base = tmp_path / "conv"
        result = _run(save_uploaded_file(base / "workspace", "new.txt", b"first"))
        assert result["path"] == "/new.txt"
        result = _run(save_uploaded_file(base / "workspace", "new.txt", b"second"))
        assert result["size"] == len(b"second")
        assert (base / "workspace" / "new.txt").read_bytes() == b"second"

        result = _run(save_uploaded_file_with_path(base / "workspace", "a/b/deep.txt", b"x"))
        assert result["path"] == "/a/b/deep.txt"
        assert (base / "workspace" / "a" / "b" / "deep.txt").read_bytes() == b"x"
