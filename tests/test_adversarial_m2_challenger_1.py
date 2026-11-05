"""Adversarial High-Concurrency Stress and Cross-Process Invalidation Test Suite.

Conducted by: teamwork_challenger_m2_1
Target: service.agentic.endpoint_abstraction.wallet_manager.WalletManager

Verification Probes:
1. High-concurrency stress harness (20 threads, 1,000+ operations performing simultaneous
   get_active_key, get_active_wallet, mark_exhausted, and external file writes).
   - Verifies: zero deadlocks, zero JSONDecodeError, zero corrupt files, zero orphaned .tmp files.
2. Cross-process mutation probe:
   - Simulates external process rewriting unstoppable_wallets.json while WalletManager
     is actively queried in a loop; verifies dynamic reload without race conditions.
3. Multi-process concurrent WalletManager instances sharing a keystore file.
4. Non-atomic / corrupt external writes resilience.
5. Invariant verification: Zero references to forbidden terms.
"""

from __future__ import annotations

import inspect
import json
import multiprocessing as mp
import os
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from service.agentic.endpoint_abstraction.wallet_manager import (
    KeystoreData,
    Wallet,
    WalletManager,
)


# Helper function for cross-process worker
def _external_process_writer(keystore_path_str: str, stop_event: Any, writes_count: Any) -> None:
    """External process that periodically updates keystore with new timestamp/wallets."""
    keystore_path = Path(keystore_path_str)
    version = 0
    while not stop_event.is_set():
        version += 1
        data = {
            "wallets": [
                {
                    "id": f"ext_wallet_{version % 5}",
                    "key": f"csk_ext_key_{version:08d}",
                    "type": "personal" if version % 2 == 0 else "org",
                    "state": "active",
                    "last_exhausted": None,
                },
                {
                    "id": "ext_wallet_backup",
                    "key": "csk_ext_backup_key_999999",
                    "type": "org",
                    "state": "active",
                    "last_exhausted": None,
                },
            ],
            "active_wallet_id": f"ext_wallet_{version % 5}",
            "updated_at": time.time(),
            "reason": f"external_update_v{version}",
        }
        # Atomic replace
        tmp = keystore_path.parent / f"ext_writer.{os.getpid()}.{time.time_ns()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, keystore_path)
            with writes_count.get_lock():
                writes_count.value += 1
        except Exception:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        time.sleep(0.005)


def _multi_process_worker(keystore_path_str: str, worker_id: int, ops_count: int, err_list: Any) -> None:
    """Independent process running WalletManager operations on shared keystore."""
    try:
        manager = WalletManager(keystore_path=keystore_path_str, auto_create=True)
        for i in range(ops_count):
            # Mix of reads and rotations
            key = manager.get_active_key()
            wallet = manager.get_active_wallet()
            if i % 15 == 0 and wallet:
                manager.mark_exhausted(wallet.id)
            elif i % 25 == 0:
                manager.reset_all()
            time.sleep(0.001)
    except Exception as exc:
        err_list.append(f"Worker {worker_id} failed: {type(exc).__name__}: {exc}")


# ============================================================================ #
# Probe 1: High-Concurrency Stress Harness (20 Threads, 1,000+ Operations)     #
# ============================================================================ #

class TestHighConcurrencyStressHarness:
    """Rigorous multi-threaded stress harness targeting WalletManager."""

    def test_20_threads_1000_plus_ops_with_external_writes(self, tmp_path: Path) -> None:
        """Execute 20 concurrent threads performing 1,200+ mixed operations.

        Simultaneously runs get_active_key(), get_active_wallet(), mark_exhausted(),
        reinstate_wallet(), check_cooldowns(), plus an external thread modifying the file.
        Verifies:
        - Zero deadlocks (completed within 15s)
        - Zero unhandled exceptions / JSONDecodeError
        - Zero corrupt files on disk
        - Zero orphaned .tmp files in keystore parent dir.
        """
        keystore_path = tmp_path / "concurrent_stress" / "unstoppable_wallets.json"
        keystore_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize pool of 10 wallets
        initial_wallets = [
            {
                "id": f"wallet_{i}",
                "key": f"csk_stress_key_{i:06d}_alpha",
                "type": "personal" if i < 3 else "org",
                "state": "active",
                "last_exhausted": None,
            }
            for i in range(10)
        ]
        initial_data = {
            "wallets": initial_wallets,
            "active_wallet_id": "wallet_0",
            "updated_at": time.time(),
        }
        keystore_path.write_text(json.dumps(initial_data, indent=2), encoding="utf-8")

        manager = WalletManager(keystore_path=keystore_path, cooldown_seconds=0.2, auto_create=True)

        errors: List[str] = []
        stop_external_writer = threading.Event()
        external_writes_done = [0]

        def external_writer_thread() -> None:
            write_idx = 0
            while not stop_external_writer.is_set():
                write_idx += 1
                new_data = {
                    "wallets": [
                        {
                            "id": f"ext_thr_wallet_{write_idx % 8}",
                            "key": f"csk_ext_thr_key_{write_idx:06d}",
                            "type": "personal",
                            "state": "active",
                            "last_exhausted": None,
                        }
                    ],
                    "active_wallet_id": f"ext_thr_wallet_{write_idx % 8}",
                    "updated_at": time.time(),
                }
                tmp = keystore_path.parent / f"thr_ext.{os.getpid()}.{time.time_ns()}.tmp"
                try:
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(new_data, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, keystore_path)
                    external_writes_done[0] += 1
                except Exception as exc:
                    errors.append(f"External writer error: {exc}")
                time.sleep(0.01)

        # Start background external disk writer
        writer_t = threading.Thread(target=external_writer_thread, daemon=True)
        writer_t.start()

        num_threads = 20
        ops_per_thread = 60  # 20 * 60 = 1,200 operations
        completed_ops = [0]
        ops_lock = threading.Lock()

        def worker(thread_idx: int) -> None:
            try:
                for op in range(ops_per_thread):
                    action = (thread_idx + op) % 6
                    if action == 0:
                        _ = manager.get_active_key()
                    elif action == 1:
                        _ = manager.get_active_wallet()
                    elif action == 2:
                        manager.mark_exhausted()
                    elif action == 3:
                        manager.mark_exhausted(f"wallet_{thread_idx % 10}")
                    elif action == 4:
                        manager.reinstate_wallet(f"wallet_{(thread_idx + 1) % 10}")
                    elif action == 5:
                        manager.check_cooldowns()

                    with ops_lock:
                        completed_ops[0] += 1

                    # Small jitter
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(f"Thread {thread_idx} error on op {op}: {type(exc).__name__}: {exc}")

        start_time = time.time()
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()

        for t in threads:
            t.join(timeout=15.0)
            if t.is_alive():
                errors.append("Deadlock detected: Worker thread timed out after 15s!")

        stop_external_writer.set()
        writer_t.join(timeout=5.0)
        duration = time.time() - start_time

        # Verify no thread errors
        assert not errors, f"Errors observed during stress run: {errors}"
        assert completed_ops[0] >= 1000, f"Expected >= 1000 ops, got {completed_ops[0]}"

        # Verify keystore file on disk is not corrupt
        assert keystore_path.exists(), "Keystore file must exist after stress run"
        raw_content = keystore_path.read_text(encoding="utf-8")
        assert len(raw_content) > 0, "Keystore file must not be 0 bytes"
        loaded_json = json.loads(raw_content)
        assert isinstance(loaded_json, dict), "Keystore payload must be a valid JSON dictionary"
        keystore_obj = KeystoreData.from_dict(loaded_json)
        assert isinstance(keystore_obj.wallets, list)

        # Verify ZERO orphaned .tmp files in keystore directory
        tmp_files = list(keystore_path.parent.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Found orphaned staging files: {tmp_files}"


# ============================================================================ #
# Probe 2: Cross-Process Dynamic Invalidation Probe                            #
# ============================================================================ #

class TestCrossProcessDynamicInvalidation:
    """Test dynamic mtime and stat reloading with real operating system processes."""

    def test_cross_process_mutation_reload_without_race(self, tmp_path: Path) -> None:
        """Simulate external process mutating keystore file while WalletManager queries in loop.

        Verifies:
        - Manager detects external changes via stat signature without needing manual reload
        - Zero race conditions, zero deadlocks, zero crashes
        - Active key dynamically tracks external updates
        """
        keystore_path = tmp_path / "cross_process" / "unstoppable_wallets.json"
        keystore_path.parent.mkdir(parents=True, exist_ok=True)

        initial_data = {
            "wallets": [
                {
                    "id": "init_wallet",
                    "key": "csk_initial_key_11111111",
                    "type": "personal",
                    "state": "active",
                    "last_exhausted": None,
                }
            ],
            "active_wallet_id": "init_wallet",
            "updated_at": time.time(),
        }
        keystore_path.write_text(json.dumps(initial_data, indent=2), encoding="utf-8")

        manager = WalletManager(keystore_path=keystore_path, auto_create=False)
        assert manager.get_active_key() == "csk_initial_key_11111111"

        stop_event = mp.Event()
        writes_count = mp.Value("i", 0)

        proc = mp.Process(
            target=_external_process_writer,
            args=(str(keystore_path), stop_event, writes_count),
        )
        proc.start()

        observed_keys: set[str] = set()
        iterations = 150
        errors: List[str] = []

        try:
            for _ in range(iterations):
                try:
                    key = manager.get_active_key()
                    if key:
                        observed_keys.add(key)
                except Exception as exc:
                    errors.append(f"Iteration error: {type(exc).__name__}: {exc}")
                time.sleep(0.005)
        finally:
            stop_event.set()
            proc.join(timeout=5.0)
            if proc.is_alive():
                proc.terminate()

        assert not errors, f"Errors during cross-process reading: {errors}"
        assert writes_count.value > 0, "External process should have performed writes"
        # Dynamic reload must have caught multiple external keys
        assert len(observed_keys) > 1, f"Expected dynamic reload to observe multiple keys, saw: {observed_keys}"

    def test_multi_process_concurrent_instances_sharing_keystore(self, tmp_path: Path) -> None:
        """4 separate OS processes running WalletManager instances on identical keystore file."""
        keystore_path = tmp_path / "multi_proc_share" / "unstoppable_wallets.json"
        keystore_path.parent.mkdir(parents=True, exist_ok=True)

        initial_wallets = [
            {
                "id": f"shared_wallet_{i}",
                "key": f"csk_shared_key_{i:04d}_live",
                "type": "personal" if i == 0 else "org",
                "state": "active",
                "last_exhausted": None,
            }
            for i in range(5)
        ]
        keystore_path.write_text(
            json.dumps({"wallets": initial_wallets, "active_wallet_id": "shared_wallet_0", "updated_at": time.time()}),
            encoding="utf-8",
        )

        manager = mp.Manager()
        error_list = manager.list()
        processes: List[mp.Process] = []

        for p_id in range(4):
            p = mp.Process(
                target=_multi_process_worker,
                args=(str(keystore_path), p_id, 50, error_list),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=15.0)
            if p.is_alive():
                p.terminate()
                error_list.append("Process timed out and had to be killed")

        assert len(error_list) == 0, f"Multi-process errors: {list(error_list)}"

        # Validate final disk state
        content = keystore_path.read_text(encoding="utf-8")
        parsed = json.loads(content)
        assert "wallets" in parsed
        assert len(parsed["wallets"]) == 5

        # Verify no stray tmp files
        stray_tmps = list(keystore_path.parent.glob("*.tmp"))
        assert len(stray_tmps) == 0, f"Stray tmp files found: {stray_tmps}"


# ============================================================================ #
# Probe 3: Hostile External Mutation & File Corruption Resilience              #
# ============================================================================ #

class TestHostileExternalMutationResilience:
    """Test behavior when external mutations corrupt disk data mid-flight."""

    def test_external_writer_temporary_corrupt_json_self_heals(self, tmp_path: Path) -> None:
        """If external process leaves invalid JSON, manager safely retains cache and recovers on fix."""
        keystore_path = tmp_path / "corrupt_test" / "unstoppable_wallets.json"
        keystore_path.parent.mkdir(parents=True, exist_ok=True)

        valid_data = {
            "wallets": [
                {"id": "w1", "key": "csk_good_key_11111111", "type": "personal", "state": "active", "last_exhausted": None}
            ],
            "active_wallet_id": "w1",
            "updated_at": 1000.0,
        }
        keystore_path.write_text(json.dumps(valid_data), encoding="utf-8")

        manager = WalletManager(keystore_path=keystore_path, auto_create=False)
        assert manager.get_active_key() == "csk_good_key_11111111"

        # 1. External process writes corrupt JSON (e.g. truncated file)
        time.sleep(0.01)
        keystore_path.write_text('{"wallets": [{"id": "w1", "key": ', encoding="utf-8")

        # Manager should not crash or raise JSONDecodeError; retains last healthy memory cache
        assert manager.get_active_key() == "csk_good_key_11111111"
        assert manager.has_active_wallet() is True

        # 2. External process restores valid JSON with updated key
        time.sleep(0.01)
        valid_data_2 = {
            "wallets": [
                {"id": "w1", "key": "csk_recovered_key_22222222", "type": "personal", "state": "active", "last_exhausted": None}
            ],
            "active_wallet_id": "w1",
            "updated_at": time.time(),
        }
        keystore_path.write_text(json.dumps(valid_data_2), encoding="utf-8")

        # Manager now detects new valid state and updates
        assert manager.get_active_key() == "csk_recovered_key_22222222"

    def test_persisted_file_permissions_stay_0600_under_stress(self, tmp_path: Path) -> None:
        """Every atomic persist preserves 0600 POSIX permissions across multiple operations."""
        keystore_path = tmp_path / "perm_test" / "unstoppable_wallets.json"
        manager = WalletManager(keystore_path=keystore_path, auto_create=True)

        for i in range(10):
            manager.add_wallet(f"csk_perm_key_{i:04d}_alpha", wallet_id=f"w_{i}")
            mode = stat.S_IMODE(keystore_path.stat().st_mode)
            assert mode & 0o777 == 0o600, f"Expected 0600, got octal {oct(mode)}"


# ============================================================================ #
# Probe 4: Adversarial Cooldown Races                                          #
# ============================================================================ #

class TestAdversarialCooldownRaces:
    """Adversarial stress on cooldown expiration boundaries."""

    def test_concurrent_threads_hitting_exact_cooldown_expiration(self, tmp_path: Path) -> None:
        """20 threads querying get_active_wallet at the instant cooldown expires do not double-reinstate."""
        keystore_path = tmp_path / "cooldown_race" / "unstoppable_wallets.json"
        keystore_path.parent.mkdir(parents=True, exist_ok=True)

        current_fake_time = [1000.0]

        def fake_time_fn() -> float:
            return current_fake_time[0]

        # Single wallet exhausted at t=1000, cooldown=10s
        data = {
            "wallets": [
                {"id": "w_cooldown", "key": "csk_cooldown_key_12345", "type": "personal", "state": "exhausted", "last_exhausted": 1000.0}
            ],
            "active_wallet_id": None,
            "updated_at": 1000.0,
        }
        keystore_path.write_text(json.dumps(data), encoding="utf-8")

        manager = WalletManager(
            keystore_path=keystore_path,
            cooldown_seconds=10.0,
            auto_create=True,
            time_fn=fake_time_fn,
        )

        assert manager.get_active_wallet() is None

        # Advance time to exact expiration threshold
        current_fake_time[0] = 1010.5

        results: List[Optional[Wallet]] = []
        res_lock = threading.Lock()

        def query_worker() -> None:
            w = manager.get_active_wallet()
            with res_lock:
                results.append(w)

        threads = [threading.Thread(target=query_worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 20 threads must agree on the reinstated wallet
        for w in results:
            assert w is not None
            assert w.id == "w_cooldown"
            assert w.state == "active"

        # Verification of on-disk state
        saved = json.loads(keystore_path.read_text(encoding="utf-8"))
        assert saved["active_wallet_id"] == "w_cooldown"
        assert saved["wallets"][0]["state"] == "active"


# ============================================================================ #
# Probe 5: Critical Invariant Audit                                            #
# ============================================================================ #

class TestCriticalInvariantsM2:
    """Strict verification of zero references to forbidden terms in Milestone 2."""

    def test_zero_forbidden_references_in_m2_codebase(self) -> None:
        """Ensure no references to forbidden term exist in any Python files or configs."""
        forbidden = "".join(["black", "hole"])

        target_files = [
            REPO_ROOT / "service" / "agentic" / "endpoint_abstraction" / "wallet_manager.py",
            REPO_ROOT / "tests" / "test_wallet_manager.py",
            Path(__file__),
        ]

        for path in target_files:
            content = path.read_text(encoding="utf-8").lower()
            # If checking this file or test_wallet_manager, ignore occurrences that are in test logic
            lines = content.splitlines()
            for line_no, line in enumerate(lines, 1):
                if forbidden in line and "forbidden" not in line and "join" not in line:
                    pytest.fail(f"Forbidden term found in {path}:{line_no}: {line.strip()}")
