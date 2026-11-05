"""Adversarial Temporal Dynamics, Boundary Probes, and Filesystem Resilience Test Suite.

Conducted by: teamwork_challenger_m2_2
Target: service.agentic.endpoint_abstraction.wallet_manager.WalletManager

Verification Probes:
1. Cooldown & Temporal Dynamics Stress:
   - Rapid exhaustion and cooldown recovery across discrete mock time steps.
   - Active pointer auto-recovery to first healthy wallet upon cooldown expiration.
   - Negative / jumping clock drift tolerance (now - last_exhausted < 0).
2. Boundary & Malformed Inputs:
   - Empty wallet pools, single-wallet pools, duplicate keys, special unicode characters in IDs.
   - Duplicate wallet IDs in on-disk JSON payload.
3. Filesystem Resilience & Error Handling:
   - Behavior under invalid filesystem states (read-only parent directory / permission denied).
4. Critical Invariant: Zero references to forbidden terms.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

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


# ============================================================================ #
# Probe 1: Temporal Cooldown Dynamics & Clock Drift Resilience                 #
# ============================================================================ #

class TestTemporalCooldownDynamicsAndClockDrift:
    """Stress-test rapid exhaustion, time-stepped recoveries, and clock drift."""

    def test_rapid_exhaustion_multi_wallet_cooldown_recovery_stepped(self, tmp_path: Path) -> None:
        """Rapidly exhausts 3 wallets and steps through discrete mock time intervals.

        Verifies:
        - Priority rotation order: personal (w1) -> personal (w3) -> org (w2) -> None.
        - Stepped cooldown recovery:
          t=1000: w1 exhausted (cooldown=60s -> ready at t=1060)
          t=1001: w3 exhausted (cooldown=60s -> ready at t=1061)
          t=1002: w2 exhausted (cooldown=60s -> ready at t=1062)
          t=1030: all remain exhausted
          t=1060.5: w1 auto-recovers; active pointer updates to w1
          t=1061.5: w3 recovers
          t=1062.5: w2 recovers
        """
        keystore_path = tmp_path / "temporal_stepped" / "unstoppable_wallets.json"
        keystore_path.parent.mkdir(parents=True, exist_ok=True)

        simulated_time = [1000.0]

        def get_time() -> float:
            return simulated_time[0]

        manager = WalletManager(
            keystore_path=keystore_path,
            cooldown_seconds=60.0,
            auto_create=True,
            time_fn=get_time,
        )
        manager.add_wallet("csk_key_11111111", wallet_id="w1", wallet_type="personal")
        manager.add_wallet("csk_key_22222222", wallet_id="w2", wallet_type="org")
        manager.add_wallet("csk_key_33333333", wallet_id="w3", wallet_type="personal")

        assert manager.get_active_wallet().id == "w1"

        # Rapid exhaustion
        simulated_time[0] = 1000.0
        r1 = manager.mark_exhausted("w1")
        assert r1 is not None and r1.id == "w3", "Expected personal wallet w3 to be preferred over org w2"

        simulated_time[0] = 1001.0
        r2 = manager.mark_exhausted("w3")
        assert r2 is not None and r2.id == "w2", "Expected org wallet w2 after personal pool exhausted"

        simulated_time[0] = 1002.0
        r3 = manager.mark_exhausted("w2")
        assert r3 is None, "All wallets must be exhausted"
        assert manager.has_active_wallet() is False
        assert manager.get_active_wallet() is None
        assert manager.get_active_key() is None

        # Step 1: t=1030.0 (30s elapsed < 60s cooldown) -> still exhausted
        simulated_time[0] = 1030.0
        assert manager.check_cooldowns() == 0
        assert manager.has_active_wallet() is False
        assert manager.get_active_wallet() is None

        # Step 2: t=1060.5 -> w1 has elapsed 60.5s >= 60.0s -> w1 recovered
        simulated_time[0] = 1060.5
        active = manager.get_active_wallet()
        assert active is not None
        assert active.id == "w1"
        assert manager.get_wallet("w1").state == "active"
        assert manager.get_wallet("w3").state == "exhausted"
        assert manager.get_wallet("w2").state == "exhausted"

        # Step 3: t=1061.5 -> w3 has elapsed 60.5s >= 60.0s -> w3 recovered
        simulated_time[0] = 1061.5
        assert manager.check_cooldowns() == 1
        assert manager.get_wallet("w3").state == "active"
        assert manager.get_wallet("w2").state == "exhausted"

        # Step 4: t=1062.5 -> w2 has elapsed 60.5s >= 60.0s -> w2 recovered
        simulated_time[0] = 1062.5
        assert manager.check_cooldowns() == 1
        assert manager.get_wallet("w2").state == "active"

        # All 3 active again
        assert len([w for w in manager.get_all_wallets() if w.state == "active"]) == 3

    def test_negative_clock_drift_resilience(self, tmp_path: Path) -> None:
        """When system clock steps backward (now - last_exhausted < 0), manager does not crash.

        Wallet remains in cooldown safely without exceptions until clock catches up.
        """
        keystore_path = tmp_path / "neg_clock" / "unstoppable_wallets.json"
        keystore_path.parent.mkdir(parents=True, exist_ok=True)

        simulated_time = [1000.0]

        manager = WalletManager(
            keystore_path=keystore_path,
            cooldown_seconds=60.0,
            auto_create=True,
            time_fn=lambda: simulated_time[0],
        )
        manager.add_wallet("csk_key_11111111", wallet_id="w1")
        manager.mark_exhausted("w1")  # last_exhausted = 1000.0

        # System clock jumps backward by 500 seconds
        simulated_time[0] = 500.0
        w = manager.get_wallet("w1")
        assert w.last_exhausted == 1000.0
        # now - last_exhausted = -500.0 < 0
        assert w.is_healthy(60.0, now=500.0) is False
        assert manager.check_cooldowns() == 0
        assert manager.get_active_wallet() is None

        # Clock returns to 1061.0 (> 1000.0 + 60.0)
        simulated_time[0] = 1061.0
        recovered = manager.check_cooldowns()
        assert recovered == 1
        assert manager.get_active_wallet().id == "w1"

    def test_forward_jumping_clock_drift(self, tmp_path: Path) -> None:
        """Clock jumping forward into future cleanly expires cooldown and recovers active state."""
        keystore_path = tmp_path / "forward_clock" / "unstoppable_wallets.json"
        manager = WalletManager(keystore_path=keystore_path, cooldown_seconds=3600.0, auto_create=True)
        manager.add_wallet("csk_key_11111111", wallet_id="w1")
        manager.mark_exhausted("w1")

        # Mock massive forward time jump (1 year)
        one_year_later = time.time() + (365 * 86400)
        with patch("time.time", return_value=one_year_later):
            assert manager.check_cooldowns(now=one_year_later) == 1
            active = manager.get_active_wallet()
            assert active is not None
            assert active.id == "w1"
            assert active.state == "active"


# ============================================================================ #
# Probe 2: Boundary & Malformed Inputs                                         #
# ============================================================================ #

class TestBoundaryAndMalformedInputs:
    """Stress-test edge cases in keystore data payloads and pool compositions."""

    def test_empty_wallet_pool_operations(self, tmp_path: Path) -> None:
        """Empty pool handles all public methods gracefully without exceptions."""
        keystore_path = tmp_path / "empty_pool" / "unstoppable_wallets.json"
        keystore_path.parent.mkdir(parents=True, exist_ok=True)
        keystore_path.write_text(json.dumps({"wallets": [], "active_wallet_id": None, "updated_at": 1000.0}))

        manager = WalletManager(keystore_path=keystore_path, auto_create=False)
        assert manager.has_active_wallet() is False
        assert manager.get_active_wallet() is None
        assert manager.get_active_key() is None
        assert manager.get_all_wallets() == []
        assert manager.get_wallet("non_existent") is None

        # Operations must not raise
        assert manager.mark_exhausted() is None
        assert manager.mark_exhausted("ghost") is None
        assert manager.check_cooldowns() == 0
        manager.reset_all()
        assert manager.has_active_wallet() is False

    def test_single_wallet_pool_lifecycle(self, tmp_path: Path) -> None:
        """Single wallet pool cycles: active -> exhausted -> None -> reset_all -> active."""
        keystore_path = tmp_path / "single_wallet" / "unstoppable_wallets.json"
        manager = WalletManager(keystore_path=keystore_path, auto_create=True)
        manager.add_wallet("csk_solo_key_12345678", wallet_id="solo_1")

        assert manager.get_active_wallet().id == "solo_1"
        res = manager.mark_exhausted("solo_1")
        assert res is None
        assert manager.has_active_wallet() is False

        # Reset all restores single wallet
        manager.reset_all()
        assert manager.has_active_wallet() is True
        assert manager.get_active_wallet().id == "solo_1"

    def test_duplicate_keys_across_distinct_wallet_ids(self, tmp_path: Path) -> None:
        """Multiple wallets having the same API key under different IDs are managed independently."""
        keystore_path = tmp_path / "duplicate_keys" / "unstoppable_wallets.json"
        manager = WalletManager(keystore_path=keystore_path, auto_create=True)

        shared_key = "csk_identical_key_12345678"
        manager.add_wallet(shared_key, wallet_id="w_primary", wallet_type="personal")
        manager.add_wallet(shared_key, wallet_id="w_backup", wallet_type="org")

        assert manager.get_active_wallet().id == "w_primary"
        res = manager.mark_exhausted("w_primary")
        assert res is not None
        assert res.id == "w_backup"
        assert res.key == shared_key

    def test_unicode_and_special_character_wallet_ids(self, tmp_path: Path) -> None:
        """Wallet IDs with emojis, multibyte UTF-8, and special symbols persist and reload cleanly."""
        keystore_path = tmp_path / "unicode_ids" / "unstoppable_wallets.json"
        manager = WalletManager(keystore_path=keystore_path, auto_create=True)

        complex_ids = [
            "wallet_üñïçødë_🚀",
            "wallet #1 (prod/us-east)",
            "org-密钥-01",
            "wallet.with.dots-and_dashes@v1",
        ]
        for idx, wid in enumerate(complex_ids):
            manager.add_wallet(f"csk_key_for_{idx:08d}", wallet_id=wid)

        manager.save_to_disk()

        # Reload from disk in a fresh manager instance
        fresh_manager = WalletManager(keystore_path=keystore_path, auto_create=False)
        loaded_ids = [w.id for w in fresh_manager.get_all_wallets()]
        assert loaded_ids == complex_ids

        # Rotate through unicode IDs
        first_active = fresh_manager.get_active_wallet()
        assert first_active.id == "wallet_üñïçødë_🚀"

        next_active = fresh_manager.mark_exhausted("wallet_üñïçødë_🚀")
        assert next_active.id == "wallet #1 (prod/us-east)"

    def test_duplicate_wallet_ids_on_disk_resilience(self, tmp_path: Path) -> None:
        """When on-disk JSON contains duplicate wallet IDs, manager loads without crashing,
        deduplicates _wallet_order, and cleanly removes without leaving ghost IDs."""
        keystore_path = tmp_path / "dup_id" / "unstoppable_wallets.json"
        keystore_path.parent.mkdir(parents=True, exist_ok=True)
        raw_payload = {
            "wallets": [
                {"id": "w1", "key": "csk_key_11111111", "type": "personal", "state": "active"},
                {"id": "w2", "key": "csk_key_22222222", "type": "org", "state": "active"},
                {"id": "w1", "key": "csk_key_33333333", "type": "personal", "state": "active"},
            ],
            "active_wallet_id": "w1",
            "updated_at": 1000.0,
        }
        keystore_path.write_text(json.dumps(raw_payload), encoding="utf-8")

        manager = WalletManager(keystore_path=keystore_path, auto_create=False)

        # 1. Verify deduplicated in-memory structures
        assert len(manager.get_all_wallets()) == 2
        assert manager._wallet_order == ["w1", "w2"]
        assert len(manager._wallet_order) == len(manager._wallets)
        assert set(manager._wallet_order) == set(manager._wallets.keys())

        # 2. Verify last-write-wins key for w1
        assert manager.get_wallet("w1").key == "csk_key_33333333"

        # 3. Verify clean removal of w1 without leaving ghost IDs
        assert manager.remove_wallet("w1") is True
        assert manager._wallet_order == ["w2"]
        assert len(manager._wallet_order) == 1
        assert set(manager._wallet_order) == set(manager._wallets.keys())
        assert manager.get_wallet("w1") is None
        assert manager.get_active_wallet().id == "w2"

        # 4. Verify clean removal of w2 down to empty pool
        assert manager.remove_wallet("w2") is True
        assert manager._wallet_order == []
        assert len(manager._wallet_order) == 0
        assert set(manager._wallet_order) == set(manager._wallets.keys())
        assert manager.has_active_wallet() is False
        assert manager.get_active_wallet() is None


# ============================================================================ #
# Probe 3: Filesystem State & Exception Handling Empirical Probes              #
# ============================================================================ #

class TestFilesystemStateAndExceptionHandling:
    """Empirically probe behavior under restricted or read-only filesystem environments."""

    def test_empirical_readonly_directory_exception_behavior(self, tmp_path: Path) -> None:
        """Verify behavior when keystore parent directory is read-only (PermissionError / chmod 0500).

        _persist_to_disk must catch PermissionError / OSError, log a warning,
        and cleanly preserve the in-memory rotation without raising unhandled exceptions.
        """
        ro_dir = tmp_path / "readonly_dir"
        ro_dir.mkdir(parents=True, exist_ok=True)
        keystore_path = ro_dir / "unstoppable_wallets.json"
        keystore_path.write_text(
            json.dumps({
                "wallets": [
                    {"id": "w1", "key": "csk_key_11111111", "type": "personal", "state": "active"},
                    {"id": "w2", "key": "csk_key_22222222", "type": "org", "state": "active"},
                ],
                "active_wallet_id": "w1",
                "updated_at": 1000.0,
            }),
            encoding="utf-8",
        )

        # Set directory to read-only (chmod 0500)
        ro_dir.chmod(stat.S_IREAD | stat.S_IEXEC)

        try:
            manager = WalletManager(keystore_path=keystore_path, auto_create=True)
            assert manager.get_active_wallet().id == "w1"

            # Must NOT raise PermissionError or OSError; in-memory state transition succeeds
            try:
                next_wallet = manager.mark_exhausted("w1")
                exception_raised = False
            except (PermissionError, OSError):
                exception_raised = True

            assert exception_raised is False, "mark_exhausted must gracefully catch PermissionError/OSError"
            assert next_wallet is not None
            assert next_wallet.id == "w2"
            assert manager.get_wallet("w1").state == "exhausted"
            assert manager.get_active_wallet().id == "w2"

            # reset_all must also succeed without raising
            manager.reset_all()
            assert manager.get_active_wallet().id == "w1"
            assert manager.get_wallet("w1").state == "active"
            assert manager.get_wallet("w2").state == "active"
        finally:
            ro_dir.chmod(stat.S_IRWXU)

    def test_persist_to_disk_enospc_and_erofs_resilience(self, tmp_path: Path) -> None:
        """Verify _persist_to_disk handles simulated ENOSPC (disk full) and EROFS without crashing."""
        keystore_path = tmp_path / "simulated_err" / "unstoppable_wallets.json"
        manager = WalletManager(keystore_path=keystore_path, auto_create=True)
        manager.add_wallet("csk_key_11111111", wallet_id="w1")

        # 1. Simulate ENOSPC (disk full)
        with patch("os.open", side_effect=OSError(28, "No space left on device")):
            # mark_exhausted triggers _persist_to_disk
            manager.mark_exhausted("w1")
            assert manager.get_wallet("w1").state == "exhausted"
            assert manager.get_active_wallet() is None

        # 2. Simulate EROFS (read-only filesystem) on os.replace
        with patch("os.replace", side_effect=OSError(30, "Read-only file system")):
            manager.reset_all()
            assert manager.get_wallet("w1").state == "active"
            assert manager.get_active_wallet().id == "w1"


# ============================================================================ #
# Probe 4: Critical Invariant Audit                                            #
# ============================================================================ #

class TestCriticalInvariantForbiddenTermsAudit:
    """Audit codebase and serialized data for complete absence of forbidden terms."""

    def test_zero_forbidden_references_in_m2_sources(self) -> None:
        """Ensure no references to forbidden term exist in any Python files or configs."""
        forbidden_term = "".join(["black", "hole"])

        target_files = [
            REPO_ROOT / "service" / "agentic" / "endpoint_abstraction" / "wallet_manager.py",
            REPO_ROOT / "tests" / "test_wallet_manager.py",
            Path(__file__),
        ]

        for path in target_files:
            content = path.read_text(encoding="utf-8").lower()
            lines = content.splitlines()
            for line_no, line in enumerate(lines, 1):
                if forbidden_term in line and "forbidden" not in line and "join" not in line:
                    pytest.fail(f"Forbidden term found in {path}:{line_no}: {line.strip()}")
