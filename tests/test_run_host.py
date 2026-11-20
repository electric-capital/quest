"""Tests for run.py's bind-address choice (``resolve_bind_host``).

Local mode enables the password-free dev login, so it listens on loopback
unless the developer opts in to the network with --host or QUEST_HOST.
"""

import pytest

import run


@pytest.mark.parametrize(
    "mode, expected",
    [("local", "127.0.0.1"), ("staging", "0.0.0.0"), ("prod", "0.0.0.0")],
)
def test_default_host_per_mode(mode, expected):
    assert run.resolve_bind_host(mode) == expected


@pytest.mark.parametrize("mode", ["local", "staging", "prod"])
def test_flag_overrides_mode_default(mode):
    assert run.resolve_bind_host(mode, host_arg="10.1.2.3") == "10.1.2.3"


@pytest.mark.parametrize("mode", ["local", "staging", "prod"])
def test_env_overrides_mode_default(mode):
    assert run.resolve_bind_host(mode, env_host="192.168.1.5") == "192.168.1.5"


def test_flag_wins_over_env():
    assert run.resolve_bind_host("local", host_arg="0.0.0.0", env_host="127.0.0.1") == "0.0.0.0"


def test_empty_values_fall_back_to_default():
    assert run.resolve_bind_host("local", host_arg="", env_host="") == "127.0.0.1"
    assert run.resolve_bind_host("prod", host_arg=None, env_host="") == "0.0.0.0"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "localhost", "LOCALHOST"])
def test_no_warning_for_loopback(host):
    assert run.local_mode_network_warning("local", host, "9000") is None


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "dev.example.com"])
def test_warning_for_local_mode_on_network(host):
    warning = run.local_mode_network_warning("local", host, "9000")
    assert warning is not None
    assert host in warning
    assert "dev login" in warning


@pytest.mark.parametrize("mode", ["staging", "prod"])
def test_no_warning_outside_local_mode(mode):
    assert run.local_mode_network_warning(mode, "0.0.0.0", "8000") is None
