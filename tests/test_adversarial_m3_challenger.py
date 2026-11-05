"""Empirical Adversarial Stress Harness for PauseManager (Milestone 3).

Authored by teamwork_challenger_m3_1.
Tests high concurrency (multithreaded and multiprocess), corrupt lockfile injection,
quarantine validation, external filesystem changes, stat cache invalidation,
strict 0600 POSIX permissions under various umasks, symlink canonicalization,
TOCTOU race handling, and zero forbidden terms.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from service.agentic.endpoint_abstraction.pause_manager import (
    LOCK_PERMISSIONS,
    PauseManager,
    PauseState,
)


# ============================================================================ #
# 1. High Concurrency: Multithreaded and Multiprocess Probing                 #
# ============================================================================ #


def test_concurrent_multithreaded_chaos(tmp_path: Path) -> None:
    """Stress test PauseManager under simultaneous multi-writer and multi-reader contention."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)

    stop_event = threading.Event()
    exceptions: List[Exception] = []

    def writer_task(worker_id: int) -> None:
        try:
            step = 0
            while not stop_event.is_set():
                if step % 3 == 0:
                    pm.pause(
                        reason=f"stress_writer_{worker_id}_{step}",
                        correlation_id=f"cid_w{worker_id}_{step}",
                        probe_pid=1000 + worker_id,
                    )
                elif step % 3 == 1:
                    pm.update_probe_pid(2000 + worker_id)
                else:
                    pm.unpause(reason=f"unpause_writer_{worker_id}_{step}")
                step += 1
                time.sleep(0.0005)
        except Exception as exc:
            exceptions.append(exc)

    def reader_task(worker_id: int) -> None:
        try:
            for _ in range(300):
                paused = pm.is_paused()
                state = pm.get_pause_state()
                if paused and state is not None:
                    assert state.paused is True
                    assert state.reason is not None
                    assert isinstance(state.paused_at, float)
                time.sleep(0.0002)
        except Exception as exc:
            exceptions.append(exc)

    # Launch 6 concurrent writers and 12 concurrent readers
    writer_threads = [
        threading.Thread(target=writer_task, args=(i,), daemon=True) for i in range(6)
    ]
    reader_threads = [
        threading.Thread(target=reader_task, args=(i,), daemon=True) for i in range(12)
    ]

    for t in writer_threads + reader_threads:
        t.start()

    for t in reader_threads:
        t.join(timeout=10.0)

    stop_event.set()
    for t in writer_threads:
        t.join(timeout=5.0)

    assert len(exceptions) == 0, f"Encountered concurrency exceptions: {exceptions}"


def _multiprocess_worker(lock_path_str: str, worker_id: int, iterations: int) -> Optional[str]:
    """Independent process worker interacting with shared PauseManager lockfile."""
    try:
        pm = PauseManager(lock_path=lock_path_str)
        for i in range(iterations):
            if i % 4 == 0:
                pm.pause(
                    reason=f"mp_worker_{worker_id}_{i}",
                    correlation_id=f"cid_mp_{worker_id}_{i}",
                    probe_pid=os.getpid(),
                )
            elif i % 4 == 1:
                pm.update_probe_pid(os.getpid())
            elif i % 4 == 2:
                _ = pm.is_paused()
                _ = pm.get_pause_state()
            else:
                pm.unpause(reason=f"mp_unpause_{worker_id}_{i}")
            time.sleep(0.001)
        return None
    except Exception as exc:
        return f"Process worker {worker_id} failed: {exc}"


def test_concurrent_multiprocess_stress(tmp_path: Path) -> None:
    """Stress test PauseManager across multiple independent operating system processes."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    lock_str = str(lock_path)

    # Pre-initialize PauseManager
    pm = PauseManager(lock_path=lock_path)
    assert not pm.is_paused()

    num_processes = 6
    iterations_per_worker = 50

    with concurrent.futures.ProcessPoolExecutor(max_workers=num_processes) as executor:
        futures = [
            executor.submit(_multiprocess_worker, lock_str, i, iterations_per_worker)
            for i in range(num_processes)
        ]
        results = [fut.result() for fut in concurrent.futures.as_completed(futures)]

    failures = [r for r in results if r is not None]
    assert len(failures) == 0, f"Multiprocess failures detected: {failures}"

    # Lock file on disk (if left paused) must be structurally valid JSON
    if lock_path.exists():
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        assert "paused" in data


# ============================================================================ #
# 2. Corrupt Lockfile Injection & Quarantine Verification                      #
# ============================================================================ #


@pytest.mark.parametrize(
    "corrupt_content,description",
    [
        ("", "0-byte empty file"),
        ("   \n\t  \n ", "whitespace-only file"),
        ('{"paused": true, "reason": ', "truncated JSON"),
        ("{invalid_json: true}", "malformed unquoted JSON"),
        ("<xml>not json</xml>", "XML content"),
        ("[1, 2, 3]", "JSON array root"),
        ('"plain string"', "JSON string root"),
        ("12345", "JSON integer root"),
        ("true", "JSON boolean root"),
        ("null", "JSON null root"),
        (b"\x00\xff\xfe\x00corrupt binary".decode("latin-1"), "binary garbage"),
    ],
)
def test_corrupt_lockfile_quarantine_and_recovery(
    tmp_path: Path, corrupt_content: str, description: str
) -> None:
    """Inject various corrupt lockfile contents and verify quarantine and auto-repair."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    lock_path.write_text(corrupt_content, encoding="utf-8")

    pm = PauseManager(lock_path=lock_path, auto_repair=True)
    assert pm.is_paused() is True

    # Check that quarantine backup was created
    corrupt_backups = list(tmp_path.glob("unstoppable_pause.lock.corrupt.*"))
    assert len(corrupt_backups) >= 1, f"Failed to quarantine: {description}"
    quarantined_text = corrupt_backups[0].read_text(encoding="utf-8")
    assert quarantined_text == corrupt_content

    # Target lockfile must be repaired to valid JSON matching schema
    assert lock_path.exists()
    disk_data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert disk_data["paused"] is True
    assert "Recovered" in disk_data["reason"] or "Corrupt" in disk_data["reason"]
    assert stat.S_IMODE(lock_path.stat().st_mode) == LOCK_PERMISSIONS


def test_corrupt_lockfile_auto_repair_disabled(tmp_path: Path) -> None:
    """When auto_repair=False, corrupt file is not overwritten but proxy safely fails closed."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    corrupt_text = "NOT_VALID_JSON"
    lock_path.write_text(corrupt_text, encoding="utf-8")

    pm = PauseManager(lock_path=lock_path, auto_repair=False)
    # Fail closed: must report paused=True without crashing
    assert pm.is_paused() is True
    state = pm.get_pause_state()
    assert state is not None
    assert state.paused is True
    assert "Corrupted lockfile" in state.reason

    # Lock file remains untouched on disk (not quarantined)
    assert lock_path.read_text(encoding="utf-8") == corrupt_text
    corrupt_backups = list(tmp_path.glob("unstoppable_pause.lock.corrupt.*"))
    assert len(corrupt_backups) == 0


def test_rapid_consecutive_corruptions_preserve_all_quarantines(tmp_path: Path) -> None:
    """Verify multiple consecutive corruptions create distinct timestamped quarantine files."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path, auto_repair=True)

    for i in range(5):
        # Inject corrupt text
        lock_path.write_text(f"corrupt_iteration_{i}", encoding="utf-8")
        # Trigger reload/quarantine
        assert pm.is_paused() is True
        time.sleep(0.005)

    backups = list(tmp_path.glob("unstoppable_pause.lock.corrupt.*"))
    assert len(backups) == 5, f"Expected 5 quarantine backups, found {len(backups)}"


@pytest.mark.parametrize(
    "payload_dict",
    [
        {},  # empty dictionary
        {"paused": True},  # missing paused_at, reason, etc.
        {"paused": False},  # explicitly unpaused
        {"paused": "true", "paused_at": "not_a_float"},  # weird types
        {"paused_at": None, "probe_pid": -50},  # negative PID and None timestamp
        {"probe_pid": "invalid_pid_string"},  # string PID
        {"reason": None, "correlation_id": None},  # null fields
    ],
)
def test_schema_resilience_missing_or_weird_fields(
    tmp_path: Path, payload_dict: Dict[str, Any]
) -> None:
    """Verify PauseState parses safely without exceptions under malformed payload fields."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    lock_path.write_text(json.dumps(payload_dict), encoding="utf-8")

    pm = PauseManager(lock_path=lock_path, auto_repair=False)
    # Must not raise exceptions
    paused = pm.is_paused()
    assert isinstance(paused, bool)
    if paused:
        state = pm.get_pause_state()
        assert state is not None
        assert isinstance(state.paused_at, float)
        assert isinstance(state.reason, str)
        if state.probe_pid is not None:
            assert isinstance(state.probe_pid, int)
            assert state.probe_pid > 0


# ============================================================================ #
# 3. External Filesystem Changes & Stat Cache Invalidation                     #
# ============================================================================ #


def test_stat_cache_invalidation_on_external_create(tmp_path: Path) -> None:
    """Verify cache invalidation when lockfile is created externally by another process."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    assert pm.is_paused() is False

    # External creation
    state = PauseState(
        paused=True,
        paused_at=1793836800.0,
        reason="External pause",
        correlation_id="req_ext_create",
    )
    lock_path.write_text(state.to_json(), encoding="utf-8")

    # Next call immediately invalidates cache and returns True
    assert pm.is_paused() is True
    cached = pm.get_pause_state()
    assert cached is not None
    assert cached.correlation_id == "req_ext_create"


def test_stat_cache_invalidation_on_external_modify(tmp_path: Path) -> None:
    """Verify cache invalidation when lockfile content is externally modified."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="initial_reason", correlation_id="cid_initial")
    assert pm.get_pause_state().reason == "initial_reason"

    # External modification with different timestamp and reason
    time.sleep(0.01)
    new_state = PauseState(
        paused=True,
        paused_at=1793839999.0,
        reason="externally_modified_reason",
        correlation_id="cid_modified",
        probe_pid=7788,
    )
    lock_path.write_text(new_state.to_json(), encoding="utf-8")

    assert pm.is_paused() is True
    updated = pm.get_pause_state()
    assert updated.reason == "externally_modified_reason"
    assert updated.correlation_id == "cid_modified"
    assert updated.probe_pid == 7788


def test_stat_cache_invalidation_on_external_unlink(tmp_path: Path) -> None:
    """Verify cache invalidation when lockfile is unlinked externally."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="unlink test")
    assert pm.is_paused() is True

    # External deletion
    lock_path.unlink()

    assert pm.is_paused() is False
    assert pm.get_pause_state() is None


def test_stat_cache_invalidation_on_inode_replacement(tmp_path: Path) -> None:
    """Verify cache invalidation when lockfile is replaced via atomic rename with a new inode."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="inode_test_1")

    # Replace via rename from temp file (new inode)
    tmp_replacement = tmp_path / "external_temp.tmp"
    repl_state = PauseState(
        paused=True,
        paused_at=time.time(),
        reason="inode_test_replaced",
        correlation_id="cid_new_inode",
    )
    tmp_replacement.write_text(repl_state.to_json(), encoding="utf-8")
    os.replace(tmp_replacement, lock_path)

    assert pm.is_paused() is True
    assert pm.get_pause_state().correlation_id == "cid_new_inode"


def test_directory_in_place_of_lockfile_resilience(tmp_path: Path) -> None:
    """Inject a directory at the lockfile location and verify quarantine and recovery."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    lock_path.mkdir(parents=True, exist_ok=True)
    assert lock_path.is_dir()

    pm = PauseManager(lock_path=lock_path, auto_repair=True)
    # The directory should be quarantined and replaced with a valid lockfile
    assert pm.is_paused() is True
    assert lock_path.is_file()
    assert json.loads(lock_path.read_text(encoding="utf-8"))["paused"] is True


def test_symlink_target_canonicalization(tmp_path: Path) -> None:
    """When lock_path is a symlink, PauseManager canonicalizes to real target."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    target_path = tmp_path / "real_target.lock"
    lock_path.symlink_to(target_path)
    assert lock_path.is_symlink()

    pm = PauseManager(lock_path=lock_path)
    assert pm.is_paused() is False

    success = pm.pause(reason="symlink target pause", correlation_id="cid_sym")
    assert success is True
    assert pm.is_paused() is True

    # Target path is created with valid JSON
    assert target_path.is_file()
    data = json.loads(target_path.read_text(encoding="utf-8"))
    assert data["paused"] is True
    assert data["correlation_id"] == "cid_sym"

    # Symlink now resolves to valid file
    assert lock_path.is_file()
    assert json.loads(lock_path.read_text(encoding="utf-8"))["correlation_id"] == "cid_sym"


def test_special_characters_and_spaces_in_path(tmp_path: Path) -> None:
    """Lockfile path containing spaces and special characters functions seamlessly."""
    lock_path = tmp_path / "path with spaces" / "special_#_lock.lock"
    pm = PauseManager(lock_path=lock_path)
    assert pm.pause(reason="spaces test") is True
    assert pm.is_paused() is True
    assert lock_path.is_file()
    assert pm.unpause() is True
    assert not lock_path.exists()


def test_monotonic_clock_backward_resilience(tmp_path: Path) -> None:
    """Clock jumping backwards (e.g. NTP drift) does not disrupt PauseManager."""
    mock_now = 2000000.0

    def time_fn() -> float:
        return mock_now

    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path, time_fn=time_fn)
    pm.pause(reason="time drift test")
    assert pm.get_pause_state().paused_at == 2000000.0

    # System clock jumps backward 1 hour
    mock_now -= 3600.0
    # Still paused
    assert pm.is_paused() is True
    # Can update PID without error
    assert pm.update_probe_pid(3344) is True


# ============================================================================ #
# 4. POSIX Permissions (0600 Mode Bits) Under Various Umasks                   #
# ============================================================================ #


@pytest.mark.parametrize("umask_val", [0o000, 0o022, 0o027, 0o077])
def test_posix_permissions_0600_across_umasks(tmp_path: Path, umask_val: int) -> None:
    """Verify lockfile retains strict 0600 mode bits regardless of process umask."""
    lock_path = tmp_path / f"unstoppable_pause_umask_{oct(umask_val)}.lock"
    orig_umask = os.umask(umask_val)
    try:
        pm = PauseManager(lock_path=lock_path)
        pm.pause(reason="umask test")

        mode = stat.S_IMODE(lock_path.stat().st_mode)
        assert mode == LOCK_PERMISSIONS, f"With umask {oct(umask_val)}, expected 0o600, got {oct(mode)}"

        # Verify update_probe_pid also preserves 0600
        pm.update_probe_pid(9876)
        mode_after_update = stat.S_IMODE(lock_path.stat().st_mode)
        assert mode_after_update == LOCK_PERMISSIONS, (
            f"After update_probe_pid with umask {oct(umask_val)}, expected 0o600, got {oct(mode_after_update)}"
        )
    finally:
        os.umask(orig_umask)


# ============================================================================ #
# 5. Check Pause Exception Integration & TOCTOU Edge Cases                    #
# ============================================================================ #


def test_check_pause_detail_structure(tmp_path: Path) -> None:
    """Verify check_pause() raises HTTPException with exact 402 detail structure."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)

    # When unpaused: check_pause does not raise
    pm.check_pause()

    # When paused: check_pause raises HTTPException(402)
    pm.pause(reason="wallets exhausted", correlation_id="req_adv_01")
    with pytest.raises(HTTPException) as exc_info:
        pm.check_pause()

    assert exc_info.value.status_code == 402
    assert "error" in exc_info.value.detail
    err = exc_info.value.detail["error"]
    assert err["code"] == 402
    assert err["status"] == "paused"
    assert err["canopy_correlation_id"] == "req_adv_01"


def test_check_pause_handles_toctou_state_none(tmp_path: Path) -> None:
    """Simulate TOCTOU race where is_paused() was True but state became None before exception creation."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="wallets exhausted")

    # Mock get_pause_state to return None (as if concurrent thread called unpause in the exact instant)
    pm.get_pause_state = lambda: None  # type: ignore[assignment]

    with pytest.raises(HTTPException) as exc_info:
        pm.check_pause()

    assert exc_info.value.status_code == 402
    assert "error" in exc_info.value.detail
    err = exc_info.value.detail["error"]
    assert err["code"] == 402
    assert err["status"] == "paused"
    # Even with state=None, a fallback correlation ID is synthesized
    assert err["canopy_correlation_id"] is not None


# ============================================================================ #
# 6. Critical Invariant: Exclusion of Forbidden Term                           #
# ============================================================================ #


def test_zero_forbidden_references_in_m3_scope() -> None:
    """Verify zero occurrences of forbidden term across all Milestone 3 files and configs."""
    forbidden = "".join(["black", "hole"])
    repo_root = Path(__file__).resolve().parent.parent

    target_files = [
        repo_root / "service" / "agentic" / "endpoint_abstraction" / "pause_manager.py",
        repo_root / "service" / "agentic" / "endpoint_abstraction" / "error_enricher.py",
        repo_root / "service" / "agentic" / "endpoint_abstraction" / "__init__.py",
        repo_root / "tool_choice_guard.py",
        repo_root / "config" / "config.yaml",
        repo_root / "tests" / "test_pause_manager.py",
        repo_root / "tests" / "test_error_enricher.py",
        repo_root / "tests" / "test_tool_choice_guard.py",
        Path(__file__),
    ]

    for fpath in target_files:
        assert fpath.exists(), f"Target file missing: {fpath}"
        text = fpath.read_text(encoding="utf-8").lower()
        for line_no, line in enumerate(text.splitlines(), 1):
            if forbidden in line and "forbidden" not in line and "join" not in line:
                pytest.fail(f"Forbidden term found in {fpath.name}:{line_no}: {line.strip()}")


def test_zero_forbidden_references_in_home_litellm_sync() -> None:
    """Verify zero occurrences of forbidden term in ~/.litellm/ synced files."""
    forbidden = "".join(["black", "hole"])
    home_litellm = Path.home() / ".litellm"

    target_files = [
        home_litellm / "config.yaml",
        home_litellm / "tool_choice_guard.py",
    ]

    for fpath in target_files:
        if fpath.exists():
            text = fpath.read_text(encoding="utf-8").lower()
            for line_no, line in enumerate(text.splitlines(), 1):
                if forbidden in line and "forbidden" not in line and "join" not in line:
                    pytest.fail(f"Forbidden term found in {fpath.name}:{line_no}: {line.strip()}")
