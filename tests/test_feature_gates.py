"""Tests for server-global feature gates.

Covers the config/feature_gates.py store (defaults, round-trip, malformed
files, flag filtering) and the admin GET/PUT endpoints in chat/routes/admin.py
(shape, toggling, admin gating, unknown-feature 404).
"""

import asyncio
import json

import pytest
from fastapi import HTTPException

import config.feature_gates as fg


ADMIN_USER = {"email": "admin@example.com"}
NON_ADMIN_USER = {"email": "user@example.com"}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def gates_file(tmp_path, monkeypatch):
    """Point the store at a per-test file (missing initially = all off)."""
    path = tmp_path / "feature_gates.json"
    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", path)
    return path


ALL_OFF = {
    f: {"enabled": False, "allowed_users": None} for f in fg.KNOWN_FEATURES
}


class TestFeatureGateStore:
    def test_everything_off_by_default(self, gates_file):
        assert fg.read_feature_gates() == ALL_OFF
        assert fg.enabled_features("user@example.com") == []
        assert not fg.is_feature_enabled(fg.FEATURE_USER_SUBAGENTS)

    def test_set_and_read_round_trip(self, gates_file):
        fg.set_feature_enabled(fg.FEATURE_USER_SUBAGENTS, True)
        assert fg.is_feature_enabled(fg.FEATURE_USER_SUBAGENTS)
        assert fg.enabled_features("user@example.com") == [
            fg.FEATURE_USER_SUBAGENTS
        ]
        persisted = json.loads(gates_file.read_text())
        # All-users gates persist in the compact legacy bool form.
        assert persisted[fg.FEATURE_USER_SUBAGENTS] is True
        # Other registered features persist as off.
        assert all(
            persisted[f] is False
            for f in fg.KNOWN_FEATURES
            if f != fg.FEATURE_USER_SUBAGENTS
        )

        fg.set_feature_enabled(fg.FEATURE_USER_SUBAGENTS, False)
        assert not fg.is_feature_enabled(fg.FEATURE_USER_SUBAGENTS)
        assert fg.enabled_features("user@example.com") == []

    def test_unknown_feature_rejected(self, gates_file):
        with pytest.raises(ValueError):
            fg.set_feature_enabled("no_such_feature", True)
        assert not fg.is_feature_enabled("no_such_feature")

    def test_malformed_file_means_all_off(self, gates_file):
        gates_file.write_text("not json {")
        assert fg.read_feature_gates() == ALL_OFF

        gates_file.write_text(json.dumps(["a", "list"]))
        assert fg.read_feature_gates() == ALL_OFF

    def test_unknown_keys_in_file_ignored(self, gates_file):
        gates_file.write_text(json.dumps({
            "retired_feature": True,
            fg.FEATURE_USER_SUBAGENTS: True,
        }))
        assert fg.read_feature_gates() == {
            **ALL_OFF,
            fg.FEATURE_USER_SUBAGENTS: {"enabled": True, "allowed_users": None},
        }
        # A write drops the unknown key.
        fg.set_feature_enabled(fg.FEATURE_USER_SUBAGENTS, True)
        assert "retired_feature" not in json.loads(gates_file.read_text())


class TestPerUserAccess:
    def test_all_users_by_default(self, gates_file):
        fg.set_feature_enabled(fg.FEATURE_DOCS, True)
        assert fg.is_feature_enabled_for_user(
            fg.FEATURE_DOCS, "anyone@example.com"
        )

    def test_allowed_list_restricts_and_normalizes(self, gates_file):
        fg.set_feature_enabled(fg.FEATURE_DOCS, True)
        fg.set_feature_allowed_users(
            fg.FEATURE_DOCS,
            ["  Alice@Example.com ", "bob@example.com", "alice@example.com", ""],
        )
        gates = fg.read_feature_gates()
        assert gates[fg.FEATURE_DOCS]["allowed_users"] == [
            "alice@example.com", "bob@example.com",
        ]
        # Case-insensitive membership check.
        assert fg.is_feature_enabled_for_user(
            fg.FEATURE_DOCS, "ALICE@example.COM"
        )
        assert not fg.is_feature_enabled_for_user(
            fg.FEATURE_DOCS, "eve@example.com"
        )
        # The coarse "on at all" check ignores the list.
        assert fg.is_feature_enabled(fg.FEATURE_DOCS)
        # enabled_features is per-user.
        assert fg.enabled_features("alice@example.com") == [
            fg.FEATURE_DOCS
        ]
        assert fg.enabled_features("eve@example.com") == []

    def test_disabled_gate_beats_allowed_list(self, gates_file):
        fg.set_feature_allowed_users(
            fg.FEATURE_DOCS, ["alice@example.com"]
        )
        assert not fg.is_feature_enabled_for_user(
            fg.FEATURE_DOCS, "alice@example.com"
        )

    def test_toggling_enabled_preserves_allowed_list(self, gates_file):
        fg.set_feature_enabled(fg.FEATURE_DOCS, True)
        fg.set_feature_allowed_users(
            fg.FEATURE_DOCS, ["alice@example.com"]
        )
        fg.set_feature_enabled(fg.FEATURE_DOCS, False)
        fg.set_feature_enabled(fg.FEATURE_DOCS, True)
        gates = fg.read_feature_gates()
        assert gates[fg.FEATURE_DOCS]["allowed_users"] == [
            "alice@example.com"
        ]

    def test_none_reopens_to_all_users(self, gates_file):
        fg.set_feature_enabled(fg.FEATURE_DOCS, True)
        fg.set_feature_allowed_users(
            fg.FEATURE_DOCS, ["alice@example.com"]
        )
        fg.set_feature_allowed_users(fg.FEATURE_DOCS, None)
        assert fg.is_feature_enabled_for_user(
            fg.FEATURE_DOCS, "eve@example.com"
        )
        # Back in compact bool form on disk.
        persisted = json.loads(gates_file.read_text())
        assert persisted[fg.FEATURE_DOCS] is True

    def test_non_per_user_feature_rejects_list(self, gates_file):
        assert fg.FEATURE_USER_SUBAGENTS not in fg.PER_USER_ACCESS_FEATURES
        with pytest.raises(ValueError, match="per-user access"):
            fg.set_feature_allowed_users(
                fg.FEATURE_USER_SUBAGENTS, ["alice@example.com"]
            )
        # None (all users) is always accepted.
        fg.set_feature_allowed_users(fg.FEATURE_USER_SUBAGENTS, None)

    def test_legacy_bool_file_reads_as_all_users(self, gates_file):
        gates_file.write_text(json.dumps({fg.FEATURE_DOCS: True}))
        assert fg.is_feature_enabled_for_user(
            fg.FEATURE_DOCS, "anyone@example.com"
        )

    def test_malformed_allowed_users_fails_closed(self, gates_file):
        gates_file.write_text(json.dumps({
            fg.FEATURE_DOCS: {
                "enabled": True, "allowed_users": "not-a-list",
            },
        }))
        assert not fg.is_feature_enabled_for_user(
            fg.FEATURE_DOCS, "anyone@example.com"
        )
        # The gate still reads as "on at all" for the admin view.
        assert fg.is_feature_enabled(fg.FEATURE_DOCS)


class TestFilterGatedFlags:
    def test_gated_flag_dropped_when_feature_off(self, gates_file):
        flags = ["nested_subagents", fg.FEATURE_USER_SUBAGENTS]
        assert fg.filter_gated_flags(flags) == ["nested_subagents"]

    def test_gated_flag_kept_when_feature_on(self, gates_file):
        fg.set_feature_enabled(fg.FEATURE_USER_SUBAGENTS, True)
        flags = ["nested_subagents", fg.FEATURE_USER_SUBAGENTS]
        assert fg.filter_gated_flags(flags) == flags

    def test_ungated_flags_pass_through(self, gates_file):
        # Flags with no matching feature key are never touched here (the
        # send path validates them against KNOWN_FLAGS separately).
        assert fg.filter_gated_flags(["nested_subagents"]) == ["nested_subagents"]
        assert fg.filter_gated_flags([]) == []


@pytest.fixture
def admin_routes(monkeypatch, gates_file):
    from chat.routes import admin

    monkeypatch.setattr(
        admin, "is_admin", lambda email: email == ADMIN_USER["email"]
    )
    return admin


class TestFeatureGateEndpoints:
    def test_list_requires_admin(self, admin_routes):
        with pytest.raises(HTTPException) as exc:
            _run(admin_routes.admin_list_feature_gates(user=NON_ADMIN_USER))
        assert exc.value.status_code == 403

    def test_update_requires_admin(self, admin_routes):
        with pytest.raises(HTTPException) as exc:
            _run(admin_routes.admin_update_feature_gate(
                fg.FEATURE_USER_SUBAGENTS,
                admin_routes.FeatureGateUpdate(enabled=True),
                user=NON_ADMIN_USER,
            ))
        assert exc.value.status_code == 403
        assert not fg.is_feature_enabled(fg.FEATURE_USER_SUBAGENTS)

    def test_list_shape_and_defaults(self, admin_routes):
        result = _run(admin_routes.admin_list_feature_gates(user=ADMIN_USER))
        assert [f["feature"] for f in result["features"]] == list(fg.KNOWN_FEATURES)
        for feature in result["features"]:
            assert feature["enabled"] is False
            assert feature["label"]
            assert feature["description"]
            assert feature["allowed_users"] is None
            assert feature["supports_user_access"] == (
                feature["feature"] in fg.PER_USER_ACCESS_FEATURES
            )

    def test_update_toggles_and_persists(self, admin_routes):
        updated = _run(admin_routes.admin_update_feature_gate(
            fg.FEATURE_USER_SUBAGENTS,
            admin_routes.FeatureGateUpdate(enabled=True),
            user=ADMIN_USER,
        ))
        assert updated["feature"] == fg.FEATURE_USER_SUBAGENTS
        assert updated["enabled"] is True
        assert fg.is_feature_enabled(fg.FEATURE_USER_SUBAGENTS)

        listed = _run(admin_routes.admin_list_feature_gates(user=ADMIN_USER))
        by_key = {f["feature"]: f for f in listed["features"]}
        assert by_key[fg.FEATURE_USER_SUBAGENTS]["enabled"] is True

        downdated = _run(admin_routes.admin_update_feature_gate(
            fg.FEATURE_USER_SUBAGENTS,
            admin_routes.FeatureGateUpdate(enabled=False),
            user=ADMIN_USER,
        ))
        assert downdated["enabled"] is False
        assert not fg.is_feature_enabled(fg.FEATURE_USER_SUBAGENTS)

    def test_update_allowed_users_round_trip(self, admin_routes):
        updated = _run(admin_routes.admin_update_feature_gate(
            fg.FEATURE_DOCS,
            admin_routes.FeatureGateUpdate(
                enabled=True, allowed_users=["Alice@Example.com"],
            ),
            user=ADMIN_USER,
        ))
        assert updated["enabled"] is True
        assert updated["allowed_users"] == ["alice@example.com"]
        assert fg.is_feature_enabled_for_user(
            fg.FEATURE_DOCS, "alice@example.com"
        )
        assert not fg.is_feature_enabled_for_user(
            fg.FEATURE_DOCS, "eve@example.com"
        )

        # Omitting allowed_users keeps the stored list.
        toggled = _run(admin_routes.admin_update_feature_gate(
            fg.FEATURE_DOCS,
            admin_routes.FeatureGateUpdate(enabled=True),
            user=ADMIN_USER,
        ))
        assert toggled["allowed_users"] == ["alice@example.com"]

        # An explicit null reopens the gate to all users.
        opened = _run(admin_routes.admin_update_feature_gate(
            fg.FEATURE_DOCS,
            admin_routes.FeatureGateUpdate(enabled=True, allowed_users=None),
            user=ADMIN_USER,
        ))
        assert opened["allowed_users"] is None
        assert fg.is_feature_enabled_for_user(
            fg.FEATURE_DOCS, "eve@example.com"
        )

    def test_update_allowed_users_rejected_for_unsupported_feature(
        self, admin_routes,
    ):
        with pytest.raises(HTTPException) as exc:
            _run(admin_routes.admin_update_feature_gate(
                fg.FEATURE_USER_SUBAGENTS,
                admin_routes.FeatureGateUpdate(
                    enabled=True, allowed_users=["alice@example.com"],
                ),
                user=ADMIN_USER,
            ))
        assert exc.value.status_code == 400
        assert not fg.is_feature_enabled(fg.FEATURE_USER_SUBAGENTS)

    def test_update_rejects_non_email_entries(self, admin_routes):
        with pytest.raises(HTTPException) as exc:
            _run(admin_routes.admin_update_feature_gate(
                fg.FEATURE_DOCS,
                admin_routes.FeatureGateUpdate(
                    enabled=True, allowed_users=["not-an-email"],
                ),
                user=ADMIN_USER,
            ))
        assert exc.value.status_code == 400
        assert not fg.is_feature_enabled(fg.FEATURE_DOCS)

    def test_update_unknown_feature_404(self, admin_routes):
        with pytest.raises(HTTPException) as exc:
            _run(admin_routes.admin_update_feature_gate(
                "no_such_feature",
                admin_routes.FeatureGateUpdate(enabled=True),
                user=ADMIN_USER,
            ))
        assert exc.value.status_code == 404


class TestRunUserSubagentGate:
    def test_validate_against_upstream_blocked_when_gate_closed(
        self, gates_file, monkeypatch
    ):
        from chat.action_request_types.run_user_subagent import (
            RunUserSubagentHandler,
        )

        handler = RunUserSubagentHandler()
        with pytest.raises(ValueError, match="disabled server-wide"):
            _run(handler.validate_against_upstream(
                {"target_user_email": "target@example.com", "prompt": "hi"},
                ADMIN_USER,
            ))

    def test_execute_blocked_when_gate_closed(self, gates_file):
        from chat.action_request_types.run_user_subagent import (
            RunUserSubagentHandler,
        )

        handler = RunUserSubagentHandler()
        with pytest.raises(ValueError, match="disabled server-wide"):
            _run(handler.execute(
                {"target_user_email": "target@example.com", "prompt": "hi"},
                ADMIN_USER,
                conversation_id="conv-1",
            ))
