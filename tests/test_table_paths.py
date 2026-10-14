"""Quest Tables path resolver: ``ChatStorage.get_table_dir``.

The ONLY table id-to-path resolver (spec #110, invariant 4), with the same
canonical-id and containment checks as ``get_doc_dir``
(tests/test_docs_storage.py) and the conversation/project resolvers
(tests/test_storage_path_resolvers.py). Ids are otherwise opaque, exactly
as for docs: no UUID format is enforced here.
"""

from __future__ import annotations

import uuid

import pytest

import chat.storage as storage_mod
import config.paths as paths_mod


# Other suites reload chat.storage, so bind through the module at call time.
def _cs():
    return storage_mod.ChatStorage


def _invalid():
    return storage_mod.InvalidStorageIdError


BAD_IDS = [
    "", ".", "..", "a/b", "../x", "x/..", "/abs", "/", "a\\b/c",
    "nul\x00byte",
]


@pytest.fixture()
def tables_root(tmp_path, monkeypatch):
    root = tmp_path / "tables"
    monkeypatch.setattr(storage_mod, "TABLES_DIR", root, raising=True)
    return root


def test_tables_dir_constant():
    assert paths_mod.TABLES_DIR == paths_mod.DATA_DIR / "tables"


class TestGetTableDir:
    def test_uuid_resolves_to_plain_join(self, tables_root):
        table_id = str(uuid.uuid4())
        assert _cs().get_table_dir(table_id) == tables_root / table_id

    def test_does_not_create_anything(self, tables_root):
        table_id = str(uuid.uuid4())
        path = _cs().get_table_dir(table_id)
        assert not path.exists()
        assert not tables_root.exists()

    def test_existing_dir_resolves(self, tables_root):
        (tables_root / "t1").mkdir(parents=True)
        assert _cs().get_table_dir("t1") == tables_root / "t1"

    @pytest.mark.parametrize("bad", BAD_IDS)
    def test_rejects_non_canonical(self, tables_root, bad):
        with pytest.raises(_invalid()):
            _cs().get_table_dir(bad)

    def test_non_string_rejected(self, tables_root):
        for value in (None, 12, b"table", uuid.uuid4()):
            with pytest.raises(_invalid()):
                _cs().get_table_dir(value)  # type: ignore[arg-type]

    def test_symlink_escaping_tables_dir_rejected(self, tables_root, tmp_path):
        tables_root.mkdir()
        outside = tmp_path / "host-secret"
        outside.mkdir()
        (tables_root / "evil").symlink_to(outside, target_is_directory=True)
        with pytest.raises(_invalid()):
            _cs().get_table_dir("evil")

    def test_dangling_symlink_pointing_outside_rejected(self, tables_root, tmp_path):
        tables_root.mkdir()
        (tables_root / "dangling").symlink_to(tmp_path / "does-not-exist")
        with pytest.raises(_invalid()):
            _cs().get_table_dir("dangling")

    def test_symlink_into_docs_dir_rejected(self, tables_root, tmp_path):
        # A sibling data root is "outside" too: tables/<id> -> docs/<id>.
        docs = tmp_path / "docs" / "d1"
        docs.mkdir(parents=True)
        tables_root.mkdir()
        (tables_root / "d1").symlink_to(docs, target_is_directory=True)
        with pytest.raises(_invalid()):
            _cs().get_table_dir("d1")

    def test_symlinked_root_is_allowed(self, tmp_path, monkeypatch):
        # A data dir that is itself a symlink (common deployment layout) must
        # not trip the containment check -- same as docs and conversations.
        real = tmp_path / "real-tables"
        (real / "t1").mkdir(parents=True)
        link = tmp_path / "tables-link"
        link.symlink_to(real, target_is_directory=True)
        monkeypatch.setattr(storage_mod, "TABLES_DIR", link, raising=True)
        assert _cs().get_table_dir("t1") == link / "t1"

    def test_invalid_id_is_a_value_error(self):
        assert issubclass(_invalid(), ValueError)


class TestTablesFeatureGate:
    def test_registered_off_by_default_and_per_user(self, tmp_path, monkeypatch):
        import config.feature_gates as fg

        monkeypatch.setattr(fg, "FEATURE_GATES_FILE", tmp_path / "feature_gates.json")
        assert fg.FEATURE_TABLES == "tables"
        assert fg.FEATURE_TABLES in fg.KNOWN_FEATURES
        assert fg.FEATURE_TABLES in fg.PER_USER_ACCESS_FEATURES
        assert fg.FEATURE_LABELS[fg.FEATURE_TABLES]["label"] == "Quest Tables"
        assert fg.FEATURE_LABELS[fg.FEATURE_TABLES]["description"]
        assert not fg.tables_enabled_for("user@example.com")

        fg.set_feature_enabled(fg.FEATURE_TABLES, True)
        assert fg.tables_enabled_for("anyone@example.com")
        fg.set_feature_allowed_users(fg.FEATURE_TABLES, ["User@Example.com"])
        assert fg.tables_enabled_for("user@example.com")
        assert not fg.tables_enabled_for("other@example.com")
        assert not fg.tables_enabled_for("")
        # Independent of the docs gate.
        assert not fg.docs_enabled_for("user@example.com")
