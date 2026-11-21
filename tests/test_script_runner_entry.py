"""Tests for the restricted sandbox entrypoint's socat port wait.

``script-runner-entry.sh`` starts the socat bridge, installs the iptables
rules while socat binds, and before dropping privileges waits for socat's
listening socket with ``wait_for_port_listen`` (bounded, fail closed). The
entrypoint returns right after defining the function when it is sourced, so
these tests drive the function against fixture ``/proc/net/tcp`` contents
without running socat or iptables.
"""

import os
import subprocess
import time
from pathlib import Path

ENTRY = Path(__file__).resolve().parent.parent / "script-runner-entry.sh"

HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
    "retrnsmt   uid  timeout inode"
)


def _row(local: str, state: str) -> str:
    return (
        f"   0: {local} 00000000:0000 {state} 00000000:00000000 00:00000000 "
        "00000000  1000        0 12345 1 0000000000000000 100 0 0 10 0"
    )


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _wait(tmp_path, port: int, pid: int, rows: list[str]) -> tuple[int, float]:
    fixture = tmp_path / "tcp"
    fixture.write_text("\n".join([HEADER, *rows]) + "\n")
    start = time.monotonic()
    result = subprocess.run(
        [
            "bash", "-c", 'source "$1"; wait_for_port_listen "$2" "$3" "$4"',
            "_", str(ENTRY), str(port), str(pid), str(fixture),
        ],
        capture_output=True,
        timeout=10,
    )
    return result.returncode, time.monotonic() - start


def test_listening_port_passes(tmp_path):
    rc, elapsed = _wait(tmp_path, 8000, os.getpid(), [_row("0100007F:1F40", "0A")])
    assert rc == 0
    assert elapsed < 0.5


def test_low_port_zero_padded(tmp_path):
    rc, _ = _wait(tmp_path, 80, os.getpid(), [_row("0100007F:0050", "0A")])
    assert rc == 0


def test_port_absent_times_out(tmp_path):
    rc, elapsed = _wait(tmp_path, 8000, os.getpid(), [_row("0100007F:1F41", "0A")])
    assert rc == 1
    assert elapsed > 0.9


def test_port_not_listening_times_out(tmp_path):
    rc, _ = _wait(tmp_path, 8000, os.getpid(), [_row("0100007F:1F40", "01")])
    assert rc == 1


def test_other_local_address_ignored(tmp_path):
    rc, _ = _wait(tmp_path, 8000, os.getpid(), [_row("00000000:1F40", "0A")])
    assert rc == 1


def test_dead_socat_fails_fast(tmp_path):
    rc, elapsed = _wait(tmp_path, 8000, _dead_pid(), [])
    assert rc == 1
    assert elapsed < 0.5


def test_executed_entrypoint_runs_the_body(tmp_path):
    """Run as a program, the guard must not skip the body: with no tools
    on PATH the iptables check is reached and fails closed."""
    empty = tmp_path / "bin"
    empty.mkdir()
    result = subprocess.run(
        ["/bin/bash", str(ENTRY), "true"],
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": str(empty), "QUEST_PORT": "1"},
    )
    assert result.returncode == 1
    assert "iptables/ip6tables unavailable, refusing to start" in result.stderr


def test_guard_only_returns_when_sourced():
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; echo after', "_", str(ENTRY)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "after"
    assert "restricted sandbox:" not in result.stderr
