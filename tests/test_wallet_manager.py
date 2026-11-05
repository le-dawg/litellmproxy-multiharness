"""Comprehensive test suite for WalletManager (Milestone 2: Phase 2 Tasks 2.1 - 2.3).

This suite tests the multi-wallet keystore, in-flight rotation, dynamic mtime cache
invalidation, atomic persistence, cooldown reinstatement, and boundary resilience.

Test Runner:
    uv run pytest tests/test_wallet_manager.py

Scope:
    Tier 1: Feature Coverage (Keystore initialization, active key/wallet resolution, rotation, schema)
    Tier 2: Boundary & Corner Cases (Missing files, 0-byte files, corrupt JSON, malformed schema,
            single/all wallet exhaustion, invalid IDs/keys, file permissions)
    Tier 3: Concurrency & Dynamic Invalidation (mtime reloading on external edit, in-memory caching,
            atomic write pattern, multi-threaded access and race-free rotations)
    Tier 4: Cooldown & Reinstatement (Temporal recovery, partial pool recovery, reset_all)
    Invariants: Zero references or dependencies on forbidden terms.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

# Ensure repository root and ~/.litellm are accessible
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path.home() / ".litellm"))
sys.path.insert(0, str(REPO_ROOT))

from service.agentic.endpoint_abstraction.wallet_manager import (
    Wallet,
    WalletManager,
)


# ============================================================================ #
# Fixtures & Helpers                                                           #
# ============================================================================ #

@pytest.fixture
def temp_keystore_path(tmp_path: Path) -> Path:
    """Return an isolated temporary keystore path."""
    return tmp_path / "litellm_test_keystore" / "unstoppable_wallets.json"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure environment variables do not leak into test runs."""
    monkeypatch.delenv("UNSTOPPABLE_WALLET_KEYS", raising=False)
    monkeypatch.delenv("UNSTOPPABLE_KEYSTORE_PATH", raising=False)
    monkeypatch.delenv("UNSTOPPABLE_CODE_KEY", raising=False)
    monkeypatch.delenv("UNSTOPPABLE_ANTHROPIC_KEY", raising=False)


@pytest.fixture
def sample_wallets_data() -> Dict[str, Any]:
    """Return a standard valid multi-wallet dictionary with 2 wallets."""
    return {
        "wallets": [
            {
                "id": "wallet_primary",
                "key": "csk_live_alpha_12345678",
                "type": "personal",
                "state": "active",
                "last_exhausted": None,
            },
            {
                "id": "wallet_backup_1",
                "key": "csk_live_backup_87654321",
                "type": "org",
                "state": "active",
                "last_exhausted": None,
            },
        ],
        "active_wallet_id": "wallet_primary",
        "updated_at": 1793836800.0,
    }


@pytest.fixture
def populated_keystore_file(temp_keystore_path: Path, sample_wallets_data: Dict[str, Any]) -> Path:
    """Create a populated keystore file on disk and return its Path."""
    temp_keystore_path.parent.mkdir(parents=True, exist_ok=True)
    temp_keystore_path.write_text(json.dumps(sample_wallets_data, indent=2), encoding="utf-8")
    return temp_keystore_path


# ============================================================================ #
# Tier 1: Feature Coverage & Happy Paths                                       #
# ============================================================================ #

class TestTier1WalletManagerCore:
    """Test standard feature coverage for WalletManager."""

    def test_init_empty_state_without_file_or_env(self, temp_keystore_path: Path, clean_env: None) -> None:
        """When keystore does not exist and env var is empty, manager initializes cleanly with empty pool."""
        manager = WalletManager(keystore_path=temp_keystore_path, env_fallback=True, auto_create=False)
        assert manager.has_active_wallet() is False
        assert manager.get_active_wallet() is None
        assert manager.get_active_key() is None
        assert manager.get_all_wallets() == []

    def test_init_from_existing_valid_keystore(self, populated_keystore_file: Path, clean_env: None) -> None:
        """Loading a pre-existing valid JSON keystore correctly parses wallets and active pointer."""
        manager = WalletManager(keystore_path=populated_keystore_file)
        assert manager.has_active_wallet() is True

        active = manager.get_active_wallet()
        assert active is not None
        assert active.id == "wallet_primary"
        assert active.key == "csk_live_alpha_12345678"
        assert active.type == "personal"
        assert active.state == "active"
        assert active.last_exhausted is None

        assert manager.get_active_key() == "csk_live_alpha_12345678"

        all_wallets = manager.get_all_wallets()
        assert len(all_wallets) == 2
        assert all_wallets[0].id == "wallet_primary"
        assert all_wallets[1].id == "wallet_backup_1"
        assert all_wallets[1].type == "org"

    def test_init_from_env_fallback(self, temp_keystore_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When keystore file is absent, manager parses comma-separated UNSTOPPABLE_WALLET_KEYS."""
        monkeypatch.setenv("UNSTOPPABLE_WALLET_KEYS", "csk_env_key_11111111, csk_env_key_22222222")
        manager = WalletManager(keystore_path=temp_keystore_path, env_fallback=True, auto_create=False)

        assert manager.has_active_wallet() is True
        wallets = manager.get_all_wallets()
        assert len(wallets) == 2
        assert wallets[0].key == "csk_env_key_11111111"
        assert wallets[1].key == "csk_env_key_22222222"
        assert manager.get_active_key() == "csk_env_key_11111111"

    def test_get_active_wallet_and_key(self, populated_keystore_file: Path) -> None:
        """get_active_wallet and get_active_key return the current active wallet details."""
        manager = WalletManager(keystore_path=populated_keystore_file)
        wallet = manager.get_active_wallet()
        key = manager.get_active_key()

        assert wallet is not None
        assert key is not None
        assert wallet.key == key
        assert wallet.id == "wallet_primary"

    def test_get_wallet_by_key(self, temp_keystore_path: Path) -> None:
        """Verify get_wallet_by_key returns matching wallet or None."""
        wm = WalletManager(keystore_path=temp_keystore_path, auto_create=True)
        wm.add_wallet("csk_key_alpha_11111111", wallet_id="w_alpha")
        wm.add_wallet("csk_key_beta_22222222", wallet_id="w_beta")

        matched = wm.get_wallet_by_key("csk_key_alpha_11111111")
        assert matched is not None
        assert matched.id == "w_alpha"

        matched_beta = wm.get_wallet_by_key("  csk_key_beta_22222222  ")
        assert matched_beta is not None
        assert matched_beta.id == "w_beta"

        assert wm.get_wallet_by_key("nonexistent_key") is None

    def test_mark_exhausted_rotates_to_next_active_wallet(self, populated_keystore_file: Path) -> None:
        """Marking primary wallet exhausted rotates active pointer to backup wallet and persists state."""
        manager = WalletManager(keystore_path=populated_keystore_file)

        # Mark primary wallet exhausted
        new_active = manager.mark_exhausted("wallet_primary")
        assert new_active is not None
        assert new_active.id == "wallet_backup_1"
        assert manager.get_active_key() == "csk_live_backup_87654321"

        # Verify on-disk file is updated
        with open(populated_keystore_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data["active_wallet_id"] == "wallet_backup_1"
        wallet_states = {w["id"]: w for w in data["wallets"]}
        assert wallet_states["wallet_primary"]["state"] == "exhausted"
        assert wallet_states["wallet_primary"]["last_exhausted"] is not None
        assert wallet_states["wallet_backup_1"]["state"] == "active"

    def test_mark_exhausted_without_args_rotates_active(self, populated_keystore_file: Path) -> None:
        """Calling mark_exhausted() with no args automatically marks the currently active wallet."""
        manager = WalletManager(keystore_path=populated_keystore_file)
        assert manager.get_active_wallet().id == "wallet_primary"

        new_active = manager.mark_exhausted()
        assert new_active is not None
        assert new_active.id == "wallet_backup_1"
        assert manager.get_active_wallet().id == "wallet_backup_1"

    def test_sequential_rotation_through_all_wallets(self, temp_keystore_path: Path) -> None:
        """Progressively exhausting 3 wallets steps through w1 -> w2 -> w3 -> None (all exhausted)."""
        data = {
            "wallets": [
                {"id": "w1", "key": "csk_key_11111111", "type": "personal", "state": "active", "last_exhausted": None},
                {"id": "w2", "key": "csk_key_22222222", "type": "org", "state": "active", "last_exhausted": None},
                {"id": "w3", "key": "csk_key_33333333", "type": "org", "state": "active", "last_exhausted": None},
            ],
            "active_wallet_id": "w1",
            "updated_at": 1000.0,
        }
        temp_keystore_path.parent.mkdir(parents=True, exist_ok=True)
        temp_keystore_path.write_text(json.dumps(data), encoding="utf-8")

        manager = WalletManager(keystore_path=temp_keystore_path)

        # Exhaust w1 -> active becomes w2
        r1 = manager.mark_exhausted("w1")
        assert r1 is not None and r1.id == "w2"
        assert manager.get_active_key() == "csk_key_22222222"

        # Exhaust w2 -> active becomes w3
        r2 = manager.mark_exhausted("w2")
        assert r2 is not None and r2.id == "w3"
        assert manager.get_active_key() == "csk_key_33333333"

        # Exhaust w3 -> all exhausted
        r3 = manager.mark_exhausted("w3")
        assert r3 is None
        assert manager.get_active_wallet() is None
        assert manager.get_active_key() is None
        assert manager.has_active_wallet() is False

    def test_keystore_schema_round_trip(self, temp_keystore_path: Path) -> None:
        """Mutating keystore and saving preserves full schema fidelity upon fresh reload."""
        manager = WalletManager(keystore_path=temp_keystore_path, auto_create=False)
        manager.add_wallet("csk_primary_key_001", wallet_type="personal", wallet_id="w_p")
        manager.add_wallet("csk_backup_key_002", wallet_type="org", wallet_id="w_b")
        manager.save_to_disk()

        fresh_manager = WalletManager(keystore_path=temp_keystore_path)
        wallets = fresh_manager.get_all_wallets()
        assert len(wallets) == 2
        assert wallets[0].id == "w_p"
        assert wallets[0].key == "csk_primary_key_001"
        assert wallets[0].type == "personal"
        assert wallets[1].id == "w_b"
        assert wallets[1].key == "csk_backup_key_002"
        assert wallets[1].type == "org"

    def test_wallet_dataclass_methods(self) -> None:
        """Test Wallet serialization, deserialization, and is_healthy helper."""
        w = Wallet(id="w_test", key="csk_test_key_12345", type="personal", state="active")
        assert w.is_healthy() is True

        d = w.to_dict()
        assert d["id"] == "w_test"
        assert d["key"] == "csk_test_key_12345"

        w_restored = Wallet.from_dict(d)
        assert w_restored.id == w.id
        assert w_restored.key == w.key
        assert w_restored.type == w.type
        assert w_restored.state == w.state


# ============================================================================ #
# Tier 2: Boundary & Corner Cases                                              #
# ============================================================================ #

class TestTier2BoundaryAndCornerCases:
    """Test boundary conditions, malformed input, and edge case resilience."""

    def test_missing_file_creates_parent_directories_on_save(self, tmp_path: Path, clean_env: None) -> None:
        """When saving to a nested path whose parent directories do not exist, manager creates them."""
        nested_keystore = tmp_path / "deep" / "nested" / "path" / "wallets.json"
        manager = WalletManager(keystore_path=nested_keystore, auto_create=False)
        manager.add_wallet("csk_brand_new_key_1234", wallet_id="w1")
        manager.save_to_disk()

        assert nested_keystore.exists()
        assert nested_keystore.is_file()

    def test_empty_zero_byte_file_handling(self, temp_keystore_path: Path, clean_env: None) -> None:
        """A 0-byte keystore file does not raise JSONDecodeError; it gracefully initializes empty state."""
        temp_keystore_path.parent.mkdir(parents=True, exist_ok=True)
        temp_keystore_path.write_text("", encoding="utf-8")
        assert temp_keystore_path.stat().st_size == 0

        manager = WalletManager(keystore_path=temp_keystore_path, auto_create=False)
        assert manager.has_active_wallet() is False
        assert manager.get_active_wallet() is None
        assert manager.get_all_wallets() == []

    def test_corrupted_json_syntax_resilience(self, temp_keystore_path: Path, clean_env: None) -> None:
        """Corrupted JSON on disk logs warning and recovers to safe empty state without crash."""
        temp_keystore_path.parent.mkdir(parents=True, exist_ok=True)
        temp_keystore_path.write_text('{"wallets": [ {"id": "w1", "key": ... corrupted syntax ...', encoding="utf-8")

        manager = WalletManager(keystore_path=temp_keystore_path, auto_create=False)
        assert manager.has_active_wallet() is False
        assert manager.get_active_wallet() is None

    @pytest.mark.parametrize(
        "malformed_payload",
        [
            {},  # completely empty object
            {"active_wallet_id": "w1"},  # missing wallets list
            {"wallets": "not-a-list", "active_wallet_id": "w1"},  # invalid wallets type
            {"wallets": [{"id": "w1"}]},  # missing key in wallet entry
            {"wallets": [{"key": "csk_key_12345"}]},  # missing id in wallet entry
            {"wallets": [{"id": "", "key": "csk_key_12345"}]},  # empty id string
            {"wallets": [{"id": "w1", "key": "short"}]},  # key too short
        ],
    )
    def test_malformed_schema_missing_required_fields(
        self, temp_keystore_path: Path, malformed_payload: Dict[str, Any], clean_env: None
    ) -> None:
        """Defensive validation handles various malformed payloads gracefully."""
        temp_keystore_path.parent.mkdir(parents=True, exist_ok=True)
        temp_keystore_path.write_text(json.dumps(malformed_payload), encoding="utf-8")

        manager = WalletManager(keystore_path=temp_keystore_path, auto_create=False)
        # Should not crash; invalid wallets are dropped or keystore re-initialized safely
        assert isinstance(manager.get_all_wallets(), list)

    def test_single_wallet_pool_exhaustion(self, temp_keystore_path: Path) -> None:
        """Single-wallet pool transitions cleanly to all-exhausted state on first failure."""
        data = {
            "wallets": [
                {"id": "w_solo", "key": "csk_solo_key_12345678", "type": "personal", "state": "active", "last_exhausted": None}
            ],
            "active_wallet_id": "w_solo",
            "updated_at": 1000.0,
        }
        temp_keystore_path.parent.mkdir(parents=True, exist_ok=True)
        temp_keystore_path.write_text(json.dumps(data), encoding="utf-8")

        manager = WalletManager(keystore_path=temp_keystore_path)
        assert manager.has_active_wallet() is True

        result = manager.mark_exhausted("w_solo")
        assert result is None
        assert manager.has_active_wallet() is False
        assert manager.get_active_wallet() is None
        assert manager.get_active_key() is None

    def test_all_wallets_exhausted_subsequent_mark_exhausted(self, populated_keystore_file: Path) -> None:
        """Calling mark_exhausted when all wallets are already exhausted returns None safely."""
        manager = WalletManager(keystore_path=populated_keystore_file)
        manager.mark_exhausted("wallet_primary")
        manager.mark_exhausted("wallet_backup_1")
        assert manager.has_active_wallet() is False

        # Subsequent call when pool is already depleted
        result = manager.mark_exhausted("wallet_backup_1")
        assert result is None
        assert manager.get_active_wallet() is None

    def test_mark_exhausted_non_existent_wallet_id(self, populated_keystore_file: Path) -> None:
        """Passing a non-existent wallet ID to mark_exhausted does not perturb active wallet."""
        manager = WalletManager(keystore_path=populated_keystore_file)
        active_before = manager.get_active_wallet()

        # Target unknown ID
        manager.mark_exhausted("unknown_ghost_wallet_id")

        active_after = manager.get_active_wallet()
        assert active_after is not None
        assert active_after.id == active_before.id
        assert active_after.state == "active"

    def test_invalid_wallet_keys_validation(self, temp_keystore_path: Path) -> None:
        """Whitespace in keys is stripped; empty or invalid keys are rejected on add."""
        manager = WalletManager(keystore_path=temp_keystore_path, auto_create=False)

        # Whitespace stripping
        w = manager.add_wallet("   csk_padded_key_123456   ", wallet_id="w_clean")
        assert w.key == "csk_padded_key_123456"

        # Rejection of empty or non-string key
        with pytest.raises((ValueError, TypeError)):
            manager.add_wallet("", wallet_id="w_empty")

        with pytest.raises((ValueError, TypeError)):
            manager.add_wallet("   ", wallet_id="w_spaces")

    def test_env_var_trailing_whitespace_and_commas(
        self, temp_keystore_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """UNSTOPPABLE_WALLET_KEYS with irregular spacing, semicolons, and empty tokens parses correctly."""
        monkeypatch.setenv("UNSTOPPABLE_WALLET_KEYS", "  csk_key_aaa11111111 , ; , csk_key_bbb22222222 ,  ")
        manager = WalletManager(keystore_path=temp_keystore_path, env_fallback=True, auto_create=False)

        wallets = manager.get_all_wallets()
        assert len(wallets) == 2
        assert wallets[0].key == "csk_key_aaa11111111"
        assert wallets[1].key == "csk_key_bbb22222222"

    def test_keystore_file_permissions_0600(self, temp_keystore_path: Path) -> None:
        """Keystore file is created with strict POSIX 0600 permissions protecting bearer tokens."""
        manager = WalletManager(keystore_path=temp_keystore_path, auto_create=False)
        manager.add_wallet("csk_secure_key_12345678", wallet_id="w_sec")
        manager.save_to_disk()

        mode = stat.S_IMODE(temp_keystore_path.stat().st_mode)
        # Verify owner read/write (0600)
        assert mode & 0o777 == 0o600

    def test_duplicate_wallet_ids_handling(self, temp_keystore_path: Path) -> None:
        """Adding a wallet with an existing ID updates the key or generates a unique ID."""
        manager = WalletManager(keystore_path=temp_keystore_path, auto_create=False)
        w1 = manager.add_wallet("csk_key_one_11111111", wallet_id="wallet_dup")
        w2 = manager.add_wallet("csk_key_two_22222222", wallet_id="wallet_dup")

        # Either updated or made unique
        wallets = manager.get_all_wallets()
        wallet_ids = [w.id for w in wallets]
        assert len(wallet_ids) == len(set(wallet_ids)), "Wallet IDs must be unique in pool"

    def test_readonly_directory_permissions_during_mark_exhausted_and_reset_all(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When keystore directory is read-only (chmod 0500), mark_exhausted and reset_all log warning and update in-memory state cleanly."""
        ro_dir = tmp_path / "readonly_dir"
        ro_dir.mkdir(parents=True, exist_ok=True)
        keystore_path = ro_dir / "unstoppable_wallets.json"
        keystore_path.write_text(
            json.dumps({
                "wallets": [
                    {
                        "id": "w_primary",
                        "key": "csk_live_alpha_11111111",
                        "type": "personal",
                        "state": "active",
                        "last_exhausted": None,
                    },
                    {
                        "id": "w_backup",
                        "key": "csk_live_backup_22222222",
                        "type": "org",
                        "state": "active",
                        "last_exhausted": None,
                    },
                ],
                "active_wallet_id": "w_primary",
                "updated_at": 1000.0,
            }),
            encoding="utf-8",
        )

        # Remove write permissions on parent directory (chmod 0500: r-x------)
        ro_dir.chmod(stat.S_IREAD | stat.S_IEXEC)
        try:
            manager = WalletManager(keystore_path=keystore_path, auto_create=True)
            assert manager.get_active_wallet().id == "w_primary"

            # 1. Test mark_exhausted under read-only directory permissions
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                next_wallet = manager.mark_exhausted("w_primary")

            # Must NOT raise PermissionError; in-memory rotation succeeds
            assert next_wallet is not None
            assert next_wallet.id == "w_backup"
            assert manager.get_active_wallet().id == "w_backup"
            assert manager.get_wallet("w_primary").state == "exhausted"
            assert manager.get_wallet("w_primary").last_exhausted is not None
            assert manager.get_wallet("w_backup").state == "active"

            # Verify warning was logged
            warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
            assert any(
                "Failed to persist keystore" in r.message or "Permission denied" in r.message
                for r in warning_records
            ), f"Expected persistence warning in logs, got: {[r.message for r in warning_records]}"

            # 2. Test reset_all under read-only directory permissions
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                manager.reset_all()

            # Must NOT raise PermissionError; in-memory reset succeeds
            assert manager.get_active_wallet().id == "w_primary"
            assert manager.get_wallet("w_primary").state == "active"
            assert manager.get_wallet("w_primary").last_exhausted is None
            assert manager.get_wallet("w_backup").state == "active"
            assert manager.get_wallet("w_backup").last_exhausted is None

            # Verify warning was logged for reset_all
            warning_records_reset = [r for r in caplog.records if r.levelno >= logging.WARNING]
            assert any(
                "Failed to persist keystore" in r.message or "Permission denied" in r.message
                for r in warning_records_reset
            ), f"Expected persistence warning in logs, got: {[r.message for r in warning_records_reset]}"
        finally:
            # Always restore write permissions so pytest tmp_path cleanup succeeds
            ro_dir.chmod(stat.S_IRWXU)

    def test_keystore_load_duplicate_wallet_ids_deduplication_and_ghost_free_removal(
        self, temp_keystore_path: Path
    ) -> None:
        """Loading a keystore file with duplicate wallet IDs deduplicates _wallet_order and allows ghost-free removal."""
        temp_keystore_path.parent.mkdir(parents=True, exist_ok=True)
        raw_payload = {
            "wallets": [
                {
                    "id": "wallet_dup",
                    "key": "csk_initial_key_11111111",
                    "type": "personal",
                    "state": "active",
                    "last_exhausted": None,
                },
                {
                    "id": "wallet_other",
                    "key": "csk_other_key_22222222",
                    "type": "org",
                    "state": "active",
                    "last_exhausted": None,
                },
                {
                    "id": "wallet_dup",
                    "key": "csk_updated_key_33333333",
                    "type": "personal",
                    "state": "active",
                    "last_exhausted": None,
                },
            ],
            "active_wallet_id": "wallet_dup",
            "updated_at": 1000.0,
        }
        temp_keystore_path.write_text(json.dumps(raw_payload), encoding="utf-8")

        manager = WalletManager(keystore_path=temp_keystore_path, auto_create=False)

        # 1. Verification on load:
        # Dictionary stores last definition
        assert manager.get_wallet("wallet_dup").key == "csk_updated_key_33333333"
        assert len(manager._wallets) == 2

        # _wallet_order must NOT contain duplicates and must match dictionary length
        assert len(manager._wallet_order) == len(manager._wallets), (
            f"_wallet_order length ({len(manager._wallet_order)}) must equal _wallets length ({len(manager._wallets)})"
        )
        assert len(manager._wallet_order) == len(set(manager._wallet_order)), (
            f"_wallet_order contains duplicates: {manager._wallet_order}"
        )
        assert manager._wallet_order == ["wallet_dup", "wallet_other"]

        # 2. Verification on remove_wallet:
        # Removing the duplicated wallet ID must remove it completely with NO ghost entries
        removed = manager.remove_wallet("wallet_dup")
        assert removed is True
        assert "wallet_dup" not in manager._wallets
        assert "wallet_dup" not in manager._wallet_order
        assert len(manager._wallet_order) == len(manager._wallets) == 1
        assert manager._wallet_order == ["wallet_other"]
        assert manager.get_active_wallet().id == "wallet_other"

        # 3. Removing remaining wallet cleans up completely
        removed_other = manager.remove_wallet("wallet_other")
        assert removed_other is True
        assert len(manager._wallet_order) == len(manager._wallets) == 0
        assert manager.get_active_wallet() is None
        assert manager.has_active_wallet() is False


# ============================================================================ #
# Tier 3: Concurrency, Dynamic Invalidation & Race Conditions                  #
# ============================================================================ #

class TestTier3ConcurrencyAndDynamicInvalidation:
    """Test dynamic mtime reloading, thread safety, and atomic file operations."""

    def test_dynamic_mtime_reload_on_external_modification(self, populated_keystore_file: Path) -> None:
        """When an external process updates the file on disk, existing manager reloads state dynamically."""
        manager = WalletManager(keystore_path=populated_keystore_file)
        assert manager.get_active_wallet().id == "wallet_primary"

        # Simulate external process (e.g. background probe or CLI) modifying keystore
        time.sleep(0.05)  # Ensure distinct mtime
        external_data = {
            "wallets": [
                {"id": "wallet_primary", "key": "csk_live_alpha_12345678", "type": "personal", "state": "exhausted", "last_exhausted": 1000.0},
                {"id": "wallet_backup_1", "key": "csk_live_backup_87654321", "type": "org", "state": "active", "last_exhausted": None},
                {"id": "wallet_replenished", "key": "csk_replenished_key_9999", "type": "personal", "state": "active", "last_exhausted": None},
            ],
            "active_wallet_id": "wallet_replenished",
            "updated_at": time.time(),
        }
        populated_keystore_file.write_text(json.dumps(external_data), encoding="utf-8")

        # Next call to manager must detect mtime change and return external active wallet
        active = manager.get_active_wallet()
        assert active is not None
        assert active.id == "wallet_replenished"
        assert manager.get_active_key() == "csk_replenished_key_9999"
        assert len(manager.get_all_wallets()) == 3

    def test_unchanged_mtime_uses_in_memory_cache(self, populated_keystore_file: Path) -> None:
        """When mtime has not changed, multiple calls do not repeatedly read or parse disk JSON."""
        manager = WalletManager(keystore_path=populated_keystore_file)

        with patch("builtins.open", side_effect=AssertionError("Disk read occurred when mtime was unchanged!")):
            # Should read strictly from in-memory cache
            for _ in range(5):
                assert manager.get_active_wallet() is not None
                assert manager.get_active_key() == "csk_live_alpha_12345678"

    def test_atomic_write_temporary_file_pattern(self, temp_keystore_path: Path) -> None:
        """Saving uses temporary file with PID/thread-id and os.replace to prevent torn reads."""
        manager = WalletManager(keystore_path=temp_keystore_path, auto_create=False)
        manager.add_wallet("csk_atomic_test_123456", wallet_id="w_atomic")

        with patch("os.replace", wraps=os.replace) as mock_replace:
            manager.save_to_disk()
            assert mock_replace.called
            src, dst = mock_replace.call_args[0]
            assert str(dst) == str(temp_keystore_path)
            assert str(src).startswith(str(temp_keystore_path.name)) or str(temp_keystore_path.stem) in str(src)

    def test_multithreaded_concurrent_access_and_rotation(self, temp_keystore_path: Path) -> None:
        """Concurrent threads calling get_active_key and mark_exhausted operate thread-safely."""
        # Create pool with 10 wallets
        wallets = [
            {"id": f"w_{i}", "key": f"csk_concurrent_key_{i:04d}", "type": "personal", "state": "active", "last_exhausted": None}
            for i in range(10)
        ]
        data = {"wallets": wallets, "active_wallet_id": "w_0", "updated_at": time.time()}
        temp_keystore_path.parent.mkdir(parents=True, exist_ok=True)
        temp_keystore_path.write_text(json.dumps(data), encoding="utf-8")

        manager = WalletManager(keystore_path=temp_keystore_path)
        errors: List[Exception] = []

        def worker_task(thread_id: int) -> None:
            try:
                for j in range(20):
                    _ = manager.get_active_key()
                    _ = manager.get_active_wallet()
                    if j % 5 == 0:
                        manager.mark_exhausted(f"w_{thread_id % 10}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker_task, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors occurred: {errors}"
        # Valid state after concurrency
        assert isinstance(manager.get_all_wallets(), list)

    def test_cross_instance_disk_synchronization(self, populated_keystore_file: Path) -> None:
        """Two manager instances bound to the same file stay synchronized via mtime."""
        manager_a = WalletManager(keystore_path=populated_keystore_file)
        manager_b = WalletManager(keystore_path=populated_keystore_file)

        assert manager_a.get_active_wallet().id == "wallet_primary"
        assert manager_b.get_active_wallet().id == "wallet_primary"

        # Manager A marks primary exhausted and saves to disk
        manager_a.mark_exhausted("wallet_primary")

        # Manager B should observe the rotation on its next call
        assert manager_b.get_active_wallet().id == "wallet_backup_1"
        assert manager_b.get_active_key() == "csk_live_backup_87654321"


# ============================================================================ #
# Tier 4: Cooldown & Reinstatement                                             #
# ============================================================================ #

class TestTier4CooldownAndReinstatement:
    """Test cooldown timing, automatic recovery, and reset capabilities."""

    def test_cooldown_expiration_and_automatic_reinstatement(self, temp_keystore_path: Path) -> None:
        """When an exhausted wallet's cooldown duration elapses, it automatically transitions to active."""
        data = {
            "wallets": [
                {
                    "id": "w_cooldown",
                    "key": "csk_cooldown_test_12345",
                    "type": "personal",
                    "state": "exhausted",
                    "last_exhausted": 1000.0,
                }
            ],
            "active_wallet_id": None,
            "updated_at": 1000.0,
        }
        temp_keystore_path.parent.mkdir(parents=True, exist_ok=True)
        temp_keystore_path.write_text(json.dumps(data), encoding="utf-8")

        # Cooldown = 60 seconds
        manager = WalletManager(keystore_path=temp_keystore_path, cooldown_seconds=60.0)

        # At t=1030 (30s elapsed < 60s): still exhausted
        with patch("time.time", return_value=1030.0):
            assert manager.has_active_wallet() is False
            assert manager.get_active_wallet() is None

        # At t=1070 (70s elapsed >= 60s): cooldown expired -> automatically reinstated
        with patch("time.time", return_value=1070.0):
            assert manager.has_active_wallet() is True
            active = manager.get_active_wallet()
            assert active is not None
            assert active.id == "w_cooldown"
            assert active.state == "active"
            assert manager.get_active_key() == "csk_cooldown_test_12345"

    def test_partial_pool_cooldown_reinstatement(self, temp_keystore_path: Path) -> None:
        """Only wallets that have exceeded cooldown are reinstated; unexpired ones remain exhausted."""
        data = {
            "wallets": [
                {"id": "w_old", "key": "csk_old_key_11111111", "type": "personal", "state": "exhausted", "last_exhausted": 1000.0},
                {"id": "w_recent", "key": "csk_recent_key_22222222", "type": "org", "state": "exhausted", "last_exhausted": 1050.0},
            ],
            "active_wallet_id": None,
            "updated_at": 1050.0,
        }
        temp_keystore_path.parent.mkdir(parents=True, exist_ok=True)
        temp_keystore_path.write_text(json.dumps(data), encoding="utf-8")

        # Cooldown = 60 seconds
        manager = WalletManager(keystore_path=temp_keystore_path, cooldown_seconds=60.0)

        # At t=1065:
        # w_old: elapsed = 65 >= 60 -> Reinstated!
        # w_recent: elapsed = 15 < 60 -> Remains exhausted
        with patch("time.time", return_value=1065.0):
            assert manager.has_active_wallet() is True
            active = manager.get_active_wallet()
            assert active.id == "w_old"

            all_wallets = {w.id: w for w in manager.get_all_wallets()}
            assert all_wallets["w_old"].state == "active"
            assert all_wallets["w_recent"].state == "exhausted"

    def test_reset_all_wallets_reinstatement(self, populated_keystore_file: Path) -> None:
        """reset_all() clears exhausted timestamps, resets all states to active, and restores active pointer."""
        manager = WalletManager(keystore_path=populated_keystore_file)
        manager.mark_exhausted("wallet_primary")
        manager.mark_exhausted("wallet_backup_1")
        assert manager.has_active_wallet() is False

        # Reset all
        manager.reset_all()

        assert manager.has_active_wallet() is True
        assert manager.get_active_wallet().id == "wallet_primary"
        assert manager.get_active_key() == "csk_live_alpha_12345678"

        for w in manager.get_all_wallets():
            assert w.state == "active"
            assert w.last_exhausted is None

    def test_rotation_prefers_active_over_exhausted(self, temp_keystore_path: Path) -> None:
        """Rotation selects next healthy active wallet, ignoring currently exhausted ones."""
        data = {
            "wallets": [
                {"id": "w1", "key": "csk_key_11111111", "type": "personal", "state": "active", "last_exhausted": None},
                {"id": "w2", "key": "csk_key_22222222", "type": "personal", "state": "exhausted", "last_exhausted": 1000.0},
                {"id": "w3", "key": "csk_key_33333333", "type": "org", "state": "active", "last_exhausted": None},
            ],
            "active_wallet_id": "w1",
            "updated_at": 1000.0,
        }
        temp_keystore_path.parent.mkdir(parents=True, exist_ok=True)
        temp_keystore_path.write_text(json.dumps(data), encoding="utf-8")

        # Set large cooldown so w2 cannot auto-recover
        manager = WalletManager(keystore_path=temp_keystore_path, cooldown_seconds=3600.0)

        with patch("time.time", return_value=1010.0):
            # Exhausting w1 should skip exhausted w2 and select active w3
            new_active = manager.mark_exhausted("w1")
            assert new_active is not None
            assert new_active.id == "w3"
            assert manager.get_active_key() == "csk_key_33333333"


# ============================================================================ #
# Critical Invariants: Zero Forbidden References                               #
# ============================================================================ #

class TestCriticalInvariants:
    """Audit code and serialized data for strict adherence to zero-forbidden-terms invariant."""

    def test_zero_forbidden_references_in_code_and_keystore(self, populated_keystore_file: Path) -> None:
        """Module source, classes, and keystore data must contain ZERO references to forbidden terms."""
        import service.agentic.endpoint_abstraction.wallet_manager as wm_mod

        forbidden_term = "".join(["black", "hole"])  # Dynamic string construction prevents false positives in test text

        # 1. Inspect module source code
        source = inspect.getsource(wm_mod)
        assert forbidden_term not in source.lower(), f"Forbidden term found in {wm_mod.__file__}"

        # 2. Inspect WalletManager class attributes & docstrings
        for attr_name in dir(WalletManager):
            attr_val = getattr(WalletManager, attr_name)
            assert forbidden_term not in str(attr_name).lower()
            if hasattr(attr_val, "__doc__") and attr_val.__doc__:
                assert forbidden_term not in str(attr_val.__doc__).lower()

        # 3. Inspect keystore JSON on disk
        manager = WalletManager(keystore_path=populated_keystore_file)
        keystore_text = populated_keystore_file.read_text(encoding="utf-8")
        assert forbidden_term not in keystore_text.lower()
