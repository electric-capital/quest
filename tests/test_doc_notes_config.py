"""Quest Doc notes foundations: the feature gate, its caps and pseudo-key.

Covers (spec issue #187, sections 4.3 and 9):

1. The ``doc_notes`` gate in config/feature_gates.py: registered, per-user,
   labelled "Quest Doc notes", off by default, and dependent on the
   ``docs`` gate through ``FEATURE_REQUIRES`` -- ``doc_notes_enabled_for``
   and ``enabled_features`` are true only while both gates are open for
   the same user (allowed-user lists intersect), while the raw switch
   (``is_feature_enabled``, ``read_feature_gates``) stays the gate's own.
2. The ``doc_notes`` connected-services pseudo-key in api/instructions.py
   (regular and public maps) follows ``doc_notes_enabled_for``, and GET
   /me's ``has_any_service_connected`` ignores it.
3. The doc-notes constants in chat/docs/constants.py.
"""

import asyncio
from unittest.mock import patch

import pytest

import config.feature_gates as fg
from chat.docs import constants as dc
from chat.docs.constants import DOC_NOTES_SERVICE_KEY, DOCS_SERVICE_KEY

ADA = "ada@example.com"
BOB = "bob@example.com"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def gates_file(tmp_path, monkeypatch):
    """Per-test feature gates file (missing initially = every gate closed)."""
    path = tmp_path / "feature_gates.json"
    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", path)
    return path


@pytest.fixture()
def no_plugins():
    """No loaded plugins, so the connected-services map is core + pseudo-keys."""
    from config import plugins as plugins_mod
    with patch.object(plugins_mod, "_LOADED", []), \
            patch("config.service_credentials.read_service_credentials",
                  return_value=None):
        yield


def _open(feature, allowed_users=None):
    fg.set_feature_enabled(feature, True)
    if allowed_users is not None:
        fg.set_feature_allowed_users(feature, allowed_users)


# ---------------------------------------------------------------------------
# 1. The feature gate
# ---------------------------------------------------------------------------


class TestGateRegistration:
    def test_registered_per_user_after_docs(self):
        assert fg.FEATURE_DOC_NOTES == "doc_notes"
        assert fg.FEATURE_DOC_NOTES in fg.KNOWN_FEATURES
        assert fg.FEATURE_DOC_NOTES in fg.PER_USER_ACCESS_FEATURES
        # Admin panel order: right after Quest Docs.
        docs_at = fg.KNOWN_FEATURES.index(fg.FEATURE_DOCS)
        assert fg.KNOWN_FEATURES[docs_at + 1] == fg.FEATURE_DOC_NOTES

    def test_label_and_description_name_the_dependency(self):
        labels = fg.FEATURE_LABELS[fg.FEATURE_DOC_NOTES]
        assert labels["label"] == "Quest Doc notes"
        assert "Requires Quest Docs" in labels["description"]

    def test_requires_docs(self):
        assert fg.FEATURE_REQUIRES[fg.FEATURE_DOC_NOTES] == fg.FEATURE_DOCS

    def test_requirements_are_known_and_acyclic(self):
        for feature, required in fg.FEATURE_REQUIRES.items():
            assert feature in fg.KNOWN_FEATURES
            assert required in fg.KNOWN_FEATURES
            seen = {feature}
            while required is not None:
                assert required not in seen, f"cycle through {required!r}"
                seen.add(required)
                required = fg.FEATURE_REQUIRES.get(required)

    def test_off_by_default(self, gates_file):
        assert fg.read_feature_gates()[fg.FEATURE_DOC_NOTES] == {
            "enabled": False, "allowed_users": None,
        }
        assert not fg.is_feature_enabled(fg.FEATURE_DOC_NOTES)
        assert not fg.doc_notes_enabled_for(ADA)


class TestDocsDependency:
    def test_doc_notes_alone_is_closed(self, gates_file):
        _open(fg.FEATURE_DOC_NOTES)
        assert not fg.doc_notes_enabled_for(ADA)
        assert not fg.is_feature_enabled_for_user(fg.FEATURE_DOC_NOTES, ADA)

    def test_docs_alone_is_closed(self, gates_file):
        _open(fg.FEATURE_DOCS)
        assert fg.docs_enabled_for(ADA)
        assert not fg.doc_notes_enabled_for(ADA)

    def test_both_open(self, gates_file):
        _open(fg.FEATURE_DOCS)
        _open(fg.FEATURE_DOC_NOTES)
        assert fg.doc_notes_enabled_for(ADA)
        # Closing docs closes notes again; reopening restores them.
        fg.set_feature_enabled(fg.FEATURE_DOCS, False)
        assert not fg.doc_notes_enabled_for(ADA)
        fg.set_feature_enabled(fg.FEATURE_DOCS, True)
        assert fg.doc_notes_enabled_for(ADA)

    def test_listed_for_notes_but_not_for_docs(self, gates_file):
        _open(fg.FEATURE_DOCS, [BOB])
        _open(fg.FEATURE_DOC_NOTES, [ADA, BOB])
        assert not fg.doc_notes_enabled_for(ADA)
        assert fg.doc_notes_enabled_for("Bob@Example.com")

    def test_listed_for_docs_but_not_for_notes(self, gates_file):
        _open(fg.FEATURE_DOCS, [ADA, BOB])
        _open(fg.FEATURE_DOC_NOTES, [BOB])
        assert fg.docs_enabled_for(ADA)
        assert not fg.doc_notes_enabled_for(ADA)
        assert fg.doc_notes_enabled_for(BOB)

    def test_docs_gate_is_not_affected_by_notes(self, gates_file):
        """The dependency runs one way: docs never requires doc_notes."""
        _open(fg.FEATURE_DOCS)
        _open(fg.FEATURE_DOC_NOTES, [BOB])
        assert fg.docs_enabled_for(ADA)

    def test_enabled_features_omits_notes_while_docs_closed(self, gates_file):
        _open(fg.FEATURE_DOC_NOTES)
        assert fg.enabled_features(ADA) == []
        _open(fg.FEATURE_DOCS, [BOB])
        assert fg.enabled_features(ADA) == []
        assert fg.enabled_features(BOB) == [fg.FEATURE_DOCS, fg.FEATURE_DOC_NOTES]
        fg.set_feature_allowed_users(fg.FEATURE_DOCS, None)
        assert fg.enabled_features(ADA) == [fg.FEATURE_DOCS, fg.FEATURE_DOC_NOTES]

    def test_raw_switch_ignores_the_dependency(self, gates_file):
        """The admin view (read_feature_gates / is_feature_enabled) shows the
        gate's own switch, even while the gate has no effect for anyone."""
        _open(fg.FEATURE_DOC_NOTES)
        assert fg.is_feature_enabled(fg.FEATURE_DOC_NOTES)
        assert fg.read_feature_gates()[fg.FEATURE_DOC_NOTES]["enabled"] is True
        assert not fg.is_feature_enabled(fg.FEATURE_DOCS)

    def test_admin_list_shows_own_switch(self, gates_file, monkeypatch):
        from chat.routes import admin
        monkeypatch.setattr(admin, "is_admin", lambda email: email == ADA)
        _open(fg.FEATURE_DOC_NOTES)
        listed = _run(admin.admin_list_feature_gates(user={"email": ADA}))
        by_key = {f["feature"]: f for f in listed["features"]}
        assert by_key[fg.FEATURE_DOC_NOTES]["enabled"] is True
        assert by_key[fg.FEATURE_DOC_NOTES]["supports_user_access"] is True
        assert by_key[fg.FEATURE_DOC_NOTES]["available"] is True
        assert by_key[fg.FEATURE_DOCS]["enabled"] is False


# ---------------------------------------------------------------------------
# 2. Connected-services pseudo-key + GET /me aggregate
# ---------------------------------------------------------------------------


def _connected(user):
    from api.instructions import get_user_connected_services
    return get_user_connected_services(user)


def _public(user):
    from api.instructions import get_public_connected_services
    return get_public_connected_services(user)


class TestPseudoKey:
    def test_is_a_pseudo_key(self):
        from api.instructions import PSEUDO_SERVICE_KEYS
        assert DOC_NOTES_SERVICE_KEY == "doc_notes"
        assert DOC_NOTES_SERVICE_KEY in PSEUDO_SERVICE_KEYS

    @pytest.mark.parametrize("services", [_connected, _public])
    def test_follows_gate_and_docs_dependency(self, services, gates_file, no_plugins):
        user = {"email": ADA}
        assert services(user)[DOC_NOTES_SERVICE_KEY] is False
        _open(fg.FEATURE_DOC_NOTES)
        assert services(user)[DOC_NOTES_SERVICE_KEY] is False
        _open(fg.FEATURE_DOCS)
        got = services(user)
        assert got[DOC_NOTES_SERVICE_KEY] is True
        assert got[DOCS_SERVICE_KEY] is True
        fg.set_feature_enabled(fg.FEATURE_DOCS, False)
        got = services(user)
        assert got[DOC_NOTES_SERVICE_KEY] is False
        assert got[DOCS_SERVICE_KEY] is False

    @pytest.mark.parametrize("services", [_connected, _public])
    def test_follows_allowed_users(self, services, gates_file, no_plugins):
        _open(fg.FEATURE_DOCS, [ADA, BOB])
        _open(fg.FEATURE_DOC_NOTES, [ADA])
        assert services({"email": "Ada@Example.com"})[DOC_NOTES_SERVICE_KEY] is True
        assert services({"email": BOB})[DOC_NOTES_SERVICE_KEY] is False
        # No email at all never matches an allowed-users list.
        assert services({})[DOC_NOTES_SERVICE_KEY] is False

    def test_not_a_connected_service_on_me(self, gates_file, no_plugins):
        import chat.routes.user as user_routes
        _open(fg.FEATURE_DOCS)
        _open(fg.FEATURE_DOC_NOTES)
        user = {"email": ADA, "name": "Ada"}
        assert _connected(user)[DOC_NOTES_SERVICE_KEY] is True
        info = _run(user_routes.get_current_user_info(user=user))
        assert info["has_any_service_connected"] is False
        assert info["enabled_features"] == [fg.FEATURE_DOCS, fg.FEATURE_DOC_NOTES]

    def test_plugin_id_is_reserved(self):
        from config.plugin_types import QuestPlugin
        from config.plugins import validate_plugin
        with pytest.raises(ValueError, match="pseudo-key"):
            validate_plugin(QuestPlugin(id="doc_notes", label="X"), set())


# ---------------------------------------------------------------------------
# 3. Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_caps(self):
        assert dc.DOC_INSTRUCTIONS_MAX_CHARS == 4_000
        assert dc.DOC_MEMORY_MAX_CHARS == 1_000
        assert dc.DOC_MAX_MEMORIES == 50
        assert dc.DOC_NOTE_MAX_PENDING_PER_DOC == 10
        assert dc.DOC_NOTE_MAX_PENDING_PER_CONVERSATION == 20
        assert dc.DOC_NOTES_DELIVERY_MAX_CHARS == 12_000
        assert dc.DOC_NOTES_PRECARD_MAX_CHARS == 3_000

    def test_suggestion_kinds(self):
        assert dc.DOC_NOTE_SUGGESTION_KINDS == ("instructions", "memory")

    def test_disabled_message(self):
        message = dc.doc_notes_disabled_message()
        assert "Quest Doc notes" in message
        assert "Settings > Features" in message
        assert message != dc.docs_disabled_message()
