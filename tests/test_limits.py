"""Admin-configurable server limits (config/limits.py) and /admin/limits."""

import asyncio
import json

import pytest
from fastapi import HTTPException

import config.limits as limits

ADMIN_USER = {"email": "admin@example.com"}
NON_ADMIN_USER = {"email": "user@example.com"}


def _run(coro):
    return asyncio.run(coro)


class TestLimitsStore:
    def test_default_when_file_absent(self):
        assert limits.read_limits() == {"max_upload_mb": limits.DEFAULT_MAX_UPLOAD_MB}
        assert limits.max_upload_bytes() == limits.DEFAULT_MAX_UPLOAD_MB * 1024 * 1024

    def test_set_and_read_round_trip(self):
        assert limits.set_max_upload_mb(500) == {"max_upload_mb": 500}
        assert limits.read_limits() == {"max_upload_mb": 500}
        assert limits.max_upload_bytes() == 500 * 1024 * 1024
        assert json.loads(limits.LIMITS_FILE.read_text()) == {"max_upload_mb": 500}

    @pytest.mark.parametrize("value", [0, -1, limits.MAX_MAX_UPLOAD_MB + 1, 1.5, True, "100"])
    def test_set_rejects_out_of_range(self, value):
        with pytest.raises(ValueError):
            limits.set_max_upload_mb(value)
        assert not limits.LIMITS_FILE.exists()

    @pytest.mark.parametrize("content", [
        "not json",
        "[1, 2]",
        '{"max_upload_mb": 0}',
        '{"max_upload_mb": "big"}',
        '{"max_upload_mb": 99999}',
    ])
    def test_malformed_file_falls_back_to_default(self, content):
        limits.LIMITS_FILE.write_text(content)
        assert limits.read_limits() == {"max_upload_mb": limits.DEFAULT_MAX_UPLOAD_MB}


@pytest.fixture
def admin_routes(monkeypatch):
    from chat.routes import admin

    monkeypatch.setattr(
        admin, "is_admin", lambda email: email == ADMIN_USER["email"]
    )
    return admin


class TestLimitsEndpoints:
    def test_get_requires_admin(self, admin_routes):
        with pytest.raises(HTTPException) as exc:
            _run(admin_routes.admin_get_limits(user=NON_ADMIN_USER))
        assert exc.value.status_code == 403

    def test_put_requires_admin(self, admin_routes):
        with pytest.raises(HTTPException) as exc:
            _run(admin_routes.admin_update_limits(
                body=admin_routes.LimitsUpdate(max_upload_mb=10),
                user=NON_ADMIN_USER,
            ))
        assert exc.value.status_code == 403
        assert not limits.LIMITS_FILE.exists()

    def test_get_reports_value_default_and_bounds(self, admin_routes):
        assert _run(admin_routes.admin_get_limits(user=ADMIN_USER)) == {
            "max_upload_mb": limits.DEFAULT_MAX_UPLOAD_MB,
            "max_upload_mb_default": limits.DEFAULT_MAX_UPLOAD_MB,
            "max_upload_mb_min": limits.MIN_MAX_UPLOAD_MB,
            "max_upload_mb_max": limits.MAX_MAX_UPLOAD_MB,
        }

    def test_put_persists(self, admin_routes):
        result = _run(admin_routes.admin_update_limits(
            body=admin_routes.LimitsUpdate(max_upload_mb=750),
            user=ADMIN_USER,
        ))
        assert result["max_upload_mb"] == 750
        assert _run(admin_routes.admin_get_limits(user=ADMIN_USER))["max_upload_mb"] == 750

    def test_put_out_of_range_is_400(self, admin_routes):
        with pytest.raises(HTTPException) as exc:
            _run(admin_routes.admin_update_limits(
                body=admin_routes.LimitsUpdate(max_upload_mb=0),
                user=ADMIN_USER,
            ))
        assert exc.value.status_code == 400
        assert exc.value.detail["error"] == "invalid_params"

    def test_app_config_reports_upload_cap(self):
        from chat.routes.user import get_app_config

        limits.set_max_upload_mb(42)
        assert _run(get_app_config())["max_upload_bytes"] == 42 * 1024 * 1024
