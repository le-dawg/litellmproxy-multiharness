"""Unit, boundary, and concurrency test suite for PauseManager.

Verifies Interface Contract 2, atomic lockfile management, 0600 permissions,
stat-signature caching, corruption quarantine & recovery, and process probing.
"""

from __future__ import annotations

import concurrent.futures
import errno
import json
import os
import re
import stat
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from service.agentic.endpoint_abstraction.pause_manager import (
    LOCK_PERMISSIONS,
    PauseManager,
    PauseState,
)


# ============================================================================ #
# Tier 1: Feature Coverage & Happy Paths                                       #
# ============================================================================ #


def test_pause_creates_valid_lock_file(tmp_path: Path) -> None:
    """PauseManager creates valid JSON lock file conforming to Interface Contract 2."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    assert not pm.is_paused()
    assert pm.get_pause_state() is None

    success = pm.pause(
        reason="personal_wallet_empty",
        correlation_id="req_test_12345",
        probe_pid=5544,
        paused_at=1793836800.0,
    )
    assert success is True
    assert pm.is_paused() is True

    state = pm.get_pause_state()
    assert state is not None
    assert state.paused is True
    assert state.paused_at == 1793836800.0
    assert state.reason == "personal_wallet_empty"
    assert state.correlation_id == "req_test_12345"
    assert state.probe_pid == 5544

    # Verify on-disk representation
    assert lock_path.is_file()
    disk_data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert disk_data["paused"] is True
    assert disk_data["paused_at"] == 1793836800.0
    assert disk_data["reason"] == "personal_wallet_empty"
    assert disk_data["correlation_id"] == "req_test_12345"
    assert disk_data["probe_pid"] == 5544


def test_lock_file_permissions_0600(tmp_path: Path) -> None:
    """Lock file must be created with strict 0600 (-rw-------) permissions."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="permission test")

    mode = stat.S_IMODE(lock_path.stat().st_mode)
    assert mode == LOCK_PERMISSIONS, f"Expected 0o600, got octal {oct(mode)}"


def test_unpause_removes_file_and_resets_state(tmp_path: Path) -> None:
    """Calling unpause() unlinks the lockfile and resets in-memory cache."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="pause before unpause")
    assert lock_path.exists()
    assert pm.is_paused() is True

    result = pm.unpause(reason="funds replenished")
    assert result is True
    assert not lock_path.exists()
    assert pm.is_paused() is False
    assert pm.get_pause_state() is None


def test_unpause_idempotent(tmp_path: Path) -> None:
    """Calling unpause() or reset() when already unpaused succeeds without error."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    assert not lock_path.exists()
    assert pm.unpause() is True
    assert pm.reset() is True
    assert pm.is_paused() is False


def test_update_probe_pid(tmp_path: Path) -> None:
    """update_probe_pid atomically updates the probe_pid field while paused."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    # When not paused, updating PID returns False
    assert pm.update_probe_pid(1234) is False

    pm.pause(reason="test pid update", correlation_id="cid_pid", probe_pid=None)
    assert pm.get_pause_state().probe_pid is None

    updated = pm.update_probe_pid(8888)
    assert updated is True
    assert pm.get_pause_state().probe_pid == 8888

    disk_data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert disk_data["probe_pid"] == 8888
    assert disk_data["correlation_id"] == "cid_pid"


# ============================================================================ #
# Tier 2: Boundary & Corner Cases (Corruption Recovery & Resilience)           #
# ============================================================================ #


def test_zero_byte_lock_file_handling(tmp_path: Path) -> None:
    """A zero-byte lockfile is detected and auto-repaired to valid paused state."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    lock_path.touch()
    assert lock_path.stat().st_size == 0

    pm = PauseManager(lock_path=lock_path, auto_repair=True)
    assert pm.is_paused() is True
    state = pm.get_pause_state()
    assert state is not None
    assert state.paused is True
    assert "Recovered from 0-byte lockfile" in state.reason

    # On disk it is now valid JSON
    disk_data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert disk_data["paused"] is True


def test_corrupt_json_quarantine_and_recovery(tmp_path: Path) -> None:
    """Corrupted/truncated JSON is quarantined to .corrupt.<ts> and recovered."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    lock_path.write_text("{\"paused\": true, INVALID_JSON", encoding="utf-8")

    pm = PauseManager(lock_path=lock_path, auto_repair=True)
    assert pm.is_paused() is True

    # Check that quarantine file was created
    corrupt_files = list(tmp_path.glob("unstoppable_pause.lock.corrupt.*"))
    assert len(corrupt_files) >= 1
    assert "INVALID_JSON" in corrupt_files[0].read_text(encoding="utf-8")

    # Target lock is now valid
    disk_data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert disk_data["paused"] is True


def test_invalid_json_types_recovery(tmp_path: Path) -> None:
    """JSON lists or primitive integers at root are quarantined and recovered."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    lock_path.write_text("[1, 2, 3]", encoding="utf-8")

    pm = PauseManager(lock_path=lock_path, auto_repair=True)
    assert pm.is_paused() is True

    corrupt_files = list(tmp_path.glob("unstoppable_pause.lock.corrupt.*"))
    assert len(corrupt_files) >= 1


def test_explicit_paused_false_payload(tmp_path: Path) -> None:
    """A valid JSON lockfile with paused=false results in is_paused() returning False."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    payload = {
        "paused": False,
        "paused_at": 1700000000.0,
        "reason": "manually unpaused",
    }
    lock_path.write_text(json.dumps(payload), encoding="utf-8")

    pm = PauseManager(lock_path=lock_path)
    assert pm.is_paused() is False
    assert pm.get_pause_state() is None


def test_permission_error_on_pause_write(tmp_path: Path) -> None:
    """Write permission errors are caught gracefully, retaining in-memory safety."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)

    with patch("os.open", side_effect=PermissionError("Permission denied")):
        res = pm.pause(reason="denied write")
        assert res is False
        # In-memory pause state is still set to prevent leaking upstream requests
        assert pm.is_paused() is True
        assert pm.get_pause_state().reason == "denied write"


def test_enospc_disk_full_on_pause_write(tmp_path: Path) -> None:
    """Disk full (ENOSPC) cleans up temporary files and avoids corrupt remnants."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)

    with patch("os.open", side_effect=OSError(errno.ENOSPC, "No space left on device")):
        res = pm.pause(reason="disk full")
        assert res is False
        # Cleaned up temp files
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0


# ============================================================================ #
# Tier 3: Caching, Invalidation & Concurrency                                  #
# ============================================================================ #


def test_stat_signature_caching_eliminates_disk_reads(tmp_path: Path) -> None:
    """Unchanged lock file does not perform repeated read_text calls."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="caching test")

    with patch.object(Path, "read_text", wraps=lock_path.read_text) as mock_read:
        for _ in range(50):
            assert pm.is_paused() is True
        # read_text should not have been called 50 times because stat signature matches
        assert mock_read.call_count == 0


def test_external_process_unlinks_lock(tmp_path: Path) -> None:
    """External deletion of lockfile is detected immediately on next is_paused() query."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="external unlink test")
    assert pm.is_paused() is True

    # Simulate external process removing the file (e.g. unstoppable_wallet_probe.sh)
    lock_path.unlink()

    assert pm.is_paused() is False
    assert pm.get_pause_state() is None


def test_external_process_creates_lock(tmp_path: Path) -> None:
    """External creation of lockfile is detected immediately on next is_paused() query."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    assert pm.is_paused() is False

    # Simulate external creation
    state = PauseState(
        paused=True,
        paused_at=1793836800.0,
        reason="External probe pause",
        correlation_id="req_ext_01",
    )
    lock_path.write_text(state.to_json(), encoding="utf-8")

    assert pm.is_paused() is True
    assert pm.get_pause_state().correlation_id == "req_ext_01"


def test_high_concurrency_multithreaded_queries(tmp_path: Path) -> None:
    """Concurrent readers and writers operate without deadlocks or corrupted states."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)

    stop_event = threading.Event()
    errors: list[Exception] = []

    def writer_worker() -> None:
        try:
            counter = 0
            while not stop_event.is_set():
                if counter % 2 == 0:
                    pm.pause(reason=f"cycle_{counter}", correlation_id=f"cid_{counter}")
                else:
                    pm.unpause()
                counter += 1
                time.sleep(0.001)
        except Exception as exc:
            errors.append(exc)

    def reader_worker() -> None:
        try:
            for _ in range(200):
                st = pm.get_pause_state()
                if st is not None:
                    assert st.paused is True
                    assert st.reason.startswith("cycle_")
                paused = pm.is_paused()
                assert isinstance(paused, bool)
                time.sleep(0.0005)
        except Exception as exc:
            errors.append(exc)

    writer_thread = threading.Thread(target=writer_worker, daemon=True)
    writer_thread.start()

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(reader_worker) for _ in range(10)]
        for fut in concurrent.futures.as_completed(futures):
            fut.result()

    stop_event.set()
    writer_thread.join(timeout=2.0)
    assert len(errors) == 0


# ============================================================================ #
# Tier 4: Probe Daemon PID & Process Lifecycle                                 #
# ============================================================================ #


def test_is_probe_running_current_pid(tmp_path: Path) -> None:
    """Current process PID is verified as alive by is_probe_running()."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="running pid", probe_pid=os.getpid())

    assert pm.is_probe_running() is True


def test_is_probe_running_dead_pid(tmp_path: Path) -> None:
    """Non-existent process PID is reported as not running."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    # Using PID that does not exist (999999)
    pm.pause(reason="dead pid", probe_pid=999999)

    assert pm.is_probe_running() is False


def test_ensure_probe_daemon_running_noop_when_alive(tmp_path: Path) -> None:
    """If probe daemon PID is active, ensure_probe_daemon_running returns PID without re-spawning."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    current_pid = os.getpid()
    pm.pause(reason="alive pid", probe_pid=current_pid)

    with patch("subprocess.Popen") as mock_popen:
        ret_pid = pm.ensure_probe_daemon_running()
        assert ret_pid == current_pid
        mock_popen.assert_not_called()


def test_ensure_probe_daemon_spawns_when_dead(tmp_path: Path) -> None:
    """If probe daemon is dead or absent, ensure_probe_daemon_running spawns detached script."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    fake_script = tmp_path / "fake_probe.sh"
    fake_script.write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
    fake_script.chmod(0o755)

    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="spawning test", probe_pid=None)

    mock_proc = MagicMock()
    mock_proc.pid = 43210
    with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
        spawned_pid = pm.ensure_probe_daemon_running(script_path=fake_script)
        assert spawned_pid == 43210
        mock_popen.assert_called_once()
        assert pm.get_pause_state().probe_pid == 43210


# ============================================================================ #
# Tier 5: Hook Integration (check_pause())                                     #
# ============================================================================ #


def test_check_pause_raises_http_402_when_paused(tmp_path: Path) -> None:
    """check_pause() raises HTTPException(402) with Interface Contract 3 payload when paused."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="credit depleted", correlation_id="req_canopy_01ABCDEF")

    with pytest.raises(HTTPException) as exc_info:
        pm.check_pause()

    assert exc_info.value.status_code == 402
    detail = exc_info.value.detail
    assert "error" in detail
    err = detail["error"]
    assert err["code"] == 402
    assert err["status"] == "paused"
    assert err["canopy_correlation_id"] == "req_canopy_01ABCDEF"
    assert "[Unstoppable AI Resource Depleted]" in err["message"]
    assert "claude --resume" in err["resume_instruction"]


def test_check_pause_noop_when_unpaused(tmp_path: Path) -> None:
    """check_pause() does not raise any exception when unpaused."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    # Does not raise
    pm.check_pause()


# ============================================================================ #
# Tier 6: Critical Invariant Audit                                             #
# ============================================================================ #


def test_zero_forbidden_references_in_pause_manager() -> None:
    """Enforce zero occurrences of forbidden term across source and tests."""
    forbidden = "".join(["black", "hole"])
    pm_source = Path(__file__).resolve().parent.parent / "service" / "agentic" / "endpoint_abstraction" / "pause_manager.py"
    test_source = Path(__file__).resolve()

    pattern = re.compile(forbidden, re.IGNORECASE)
    assert not pattern.search(pm_source.read_text(encoding="utf-8")), "Forbidden term found in pause_manager.py"
    assert not pattern.search(test_source.read_text(encoding="utf-8")), "Forbidden term found in test_pause_manager.py"
