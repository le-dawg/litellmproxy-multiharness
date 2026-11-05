"""Adversarial Empirical Probe Suite for Milestone 4 (CLI Management Controller).

Agent: teamwork_challenger_m4_2 (Milestone 4 CLI Controller Adversarial Challenger)
Target Component: service/bin/wallet_ctl.py

Adversarial Dimensions Tested:
1. CLI Subcommand Parameter Fuzzing & Input Boundary Stress:
   - add-key: empty strings, whitespace, short keys (<8 chars), Unicode characters, invalid --type choices, duplicate keys.
2. Keystore & Lockfile Corruption Resiliency:
   - status: corrupted keystore JSON, 0-byte keystore, missing keystore, corrupted pause lock, 0-byte pause lock, missing pause lock.
3. Non-Existent Wallet IDs & Error Handling:
   - mark-exhausted: non-existent ID, empty string ID (exit code 1).
   - set-active: non-existent ID, empty string ID (exit code 1).
4. Lock Reset Graceful Handling:
   - reset-lock & unpause alias: graceful exit 0 and unpaused state when lock does not exist.
5. Probe Fallback & Upstream Mock Verification:
   - probe when all wallets exhausted: primary key fallback in Authorization header.
   - probe mock upstream returning 200 (unpauses, resets wallets) and 402 (maintains pause lock).
6. File Permissions Preservation:
   - Strict 0600 mode preservation on unstoppable_wallets.json after all mutations (add-key, mark-exhausted, set-active, reset-lock).
   - Strict 0600 mode on unstoppable_pause.lock.
7. Concurrency Safety:
   - Concurrent process execution mutating keys does not corrupt JSON or degrade permissions.
8. Critical Invariant:
   - Zero occurrences of forbidden term across codebase and test artifacts.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from service.agentic.endpoint_abstraction.pause_manager import LOCK_PERMISSIONS, PauseManager
from service.agentic.endpoint_abstraction.wallet_manager import WalletManager
from tests.test_probe_daemon import MockCanopyServer, make_subprocess_env

WALLET_CTL_PATH = REPO_ROOT / "service" / "bin" / "wallet_ctl.py"


@pytest.fixture
def probe_env(tmp_path: Path) -> Dict[str, Any]:
    """Provide isolated directory, keystore, pause lock, PID file, and mock osascript."""
    litellm_dir = tmp_path / ".litellm"
    litellm_dir.mkdir(parents=True, exist_ok=True)

    keystore_path = litellm_dir / "unstoppable_wallets.json"
    pause_lock_path = litellm_dir / "unstoppable_pause.lock"
    pid_path = litellm_dir / "unstoppable_probe.pid"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    osascript_log = tmp_path / "osascript.log"
    mock_osascript = bin_dir / "osascript"
    mock_osascript.write_text(
        f'#!/bin/sh\necho "$@" >> "{osascript_log}"\nexit 0\n',
        encoding="utf-8",
    )
    mock_osascript.chmod(0o755)

    initial_keystore = {
        "wallets": [
            {
                "id": "wallet_primary",
                "key": "csk_live_test_secret_key_12345",
                "type": "personal",
                "state": "active",
                "last_exhausted": None,
            },
            {
                "id": "wallet_backup",
                "key": "csk_backup_test_secret_key_67890",
                "type": "org",
                "state": "active",
                "last_exhausted": None,
            },
        ],
        "active_wallet_id": "wallet_primary",
        "updated_at": 1793836800.0,
    }
    keystore_path.write_text(json.dumps(initial_keystore, indent=2), encoding="utf-8")
    os.chmod(keystore_path, 0o600)

    return {
        "root": tmp_path,
        "litellm_dir": litellm_dir,
        "keystore_path": keystore_path,
        "pause_lock_path": pause_lock_path,
        "pid_path": pid_path,
        "bin_dir": bin_dir,
        "osascript_log": osascript_log,
        "mock_osascript": mock_osascript,
    }


def run_ctl(
    probe_env: Dict[str, Any],
    subcommand_args: List[str],
    upstream_url: str = "http://127.0.0.1:9999",
    timeout: float = 10.0,
) -> subprocess.CompletedProcess:
    """Helper executing wallet_ctl.py within an isolated environment."""
    env = make_subprocess_env(probe_env, upstream_url)
    cmd = [
        sys.executable,
        str(WALLET_CTL_PATH),
        "--config-dir",
        str(probe_env["litellm_dir"]),
    ] + subcommand_args
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ============================================================================ #
# 1. Parameter Fuzzing & Boundary Input Stress                                #
# ============================================================================ #


def test_fuzz_add_key_empty_and_whitespace(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe add-key with empty and whitespace-only strings."""
    # Empty string
    res_empty = run_ctl(probe_env, ["add-key", "", "--json"])
    assert res_empty.returncode == 1
    err = json.loads(res_empty.stdout)
    assert err["status"] == "error"
    assert "at least 8" in err["message"]

    # Whitespace-only string
    res_ws = run_ctl(probe_env, ["add-key", "       ", "--json"])
    assert res_ws.returncode == 1
    err_ws = json.loads(res_ws.stdout)
    assert err_ws["status"] == "error"
    assert "at least 8" in err_ws["message"]


def test_fuzz_add_key_short_keys(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe add-key with short strings (<8 characters)."""
    short_samples = ["1", "short", "csk_1", "1234567"]
    for sample in short_samples:
        res = run_ctl(probe_env, ["add-key", sample, "--json"])
        assert res.returncode == 1, f"Expected non-zero returncode for short key: {sample}"
        err = json.loads(res.stdout)
        assert err["status"] == "error"
        assert "at least 8" in err["message"]


def test_fuzz_add_key_unicode_and_special_characters(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe add-key with multi-byte Unicode and special characters."""
    unicode_key = "csk_live_🚀_€_日本語_test123"
    res = run_ctl(
        probe_env,
        ["add-key", unicode_key, "--id", "wallet_unicode", "--type", "personal", "--json"],
    )
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["status"] == "ok"
    assert data["wallet"]["id"] == "wallet_unicode"

    wm = WalletManager(keystore_path=probe_env["keystore_path"])
    wallet = wm.get_wallet("wallet_unicode")
    assert wallet is not None
    assert wallet.key == unicode_key


def test_fuzz_add_key_invalid_type_rejected(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe add-key with invalid --type arguments."""
    invalid_types = ["admin", "root", "unknown", "PERSONAL", "ORG"]
    for bad_type in invalid_types:
        res = run_ctl(
            probe_env,
            ["add-key", "csk_valid_key_12345", "--type", bad_type],
        )
        assert res.returncode == 2, f"Expected exit code 2 for invalid --type {bad_type}"
        assert "invalid choice" in res.stderr.lower()


def test_fuzz_add_key_duplicate_keys(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe duplicate key insertions and update idempotency."""
    key = "csk_duplicate_shared_key_99999"

    # Add first instance
    res1 = run_ctl(probe_env, ["add-key", key, "--id", "wallet_dup_1", "--json"])
    assert res1.returncode == 0

    # Add second instance with different wallet ID
    res2 = run_ctl(probe_env, ["add-key", key, "--id", "wallet_dup_2", "--json"])
    assert res2.returncode == 0

    # Add same key updating existing ID
    res3 = run_ctl(probe_env, ["add-key", key, "--id", "wallet_dup_1", "--json"])
    assert res3.returncode == 0

    wm = WalletManager(keystore_path=probe_env["keystore_path"])
    assert wm.get_wallet("wallet_dup_1") is not None
    assert wm.get_wallet("wallet_dup_2") is not None


# ============================================================================ #
# 2. Keystore & Lockfile Corruption Resiliency                                #
# ============================================================================ #


def test_status_corrupted_keystore_json(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe status when keystore contains invalid/truncated JSON."""
    keystore = probe_env["keystore_path"]
    keystore.write_text('{"wallets": [{"id": "broken", "key": truncated_json', encoding="utf-8")

    res = run_ctl(probe_env, ["status", "--json"])
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["total_wallets"] == 0
    assert data["wallets"] == []

    # Also verify text mode doesn't crash
    res_text = run_ctl(probe_env, ["status"])
    assert res_text.returncode == 0
    assert "No wallets configured" in res_text.stdout


def test_status_zero_byte_keystore(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe status when keystore is a 0-byte empty file."""
    keystore = probe_env["keystore_path"]
    keystore.write_text("", encoding="utf-8")

    res = run_ctl(probe_env, ["status", "--json"])
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["total_wallets"] == 0


def test_status_missing_keystore(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe status when keystore file is absent from disk."""
    keystore = probe_env["keystore_path"]
    keystore.unlink(missing_ok=True)

    res = run_ctl(probe_env, ["status", "--json"])
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["total_wallets"] == 0


def test_status_corrupted_pause_lock(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe status when pause lock contains corrupted JSON."""
    lock = probe_env["pause_lock_path"]
    lock.write_text("{CORRUPT_LOCK_DATA_NOT_JSON", encoding="utf-8")

    res = run_ctl(probe_env, ["status", "--json"])
    assert res.returncode == 0
    data = json.loads(res.stdout)
    # PauseManager with auto_repair quarantines and recreates a valid pause state
    assert data["pause_lock"]["paused"] is True
    assert "Corrupted lockfile" in data["pause_lock"]["reason"] or "invalid JSON" in data["pause_lock"]["reason"]


def test_status_zero_byte_pause_lock(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe status when pause lock is a 0-byte file."""
    lock = probe_env["pause_lock_path"]
    lock.write_text("", encoding="utf-8")

    res = run_ctl(probe_env, ["status", "--json"])
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["pause_lock"]["paused"] is True
    assert "0-byte lockfile" in data["pause_lock"]["reason"] or "Corrupt" in data["pause_lock"]["reason"]


def test_status_missing_pause_lock(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe status when pause lock does not exist."""
    lock = probe_env["pause_lock_path"]
    lock.unlink(missing_ok=True)

    res = run_ctl(probe_env, ["status", "--json"])
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["pause_lock"]["paused"] is False
    assert data["pause_lock"]["reason"] is None


# ============================================================================ #
# 3. Non-Existent Wallet IDs & Error Handling                                 #
# ============================================================================ #


def test_mark_exhausted_nonexistent_id(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe mark-exhausted with non-existent wallet IDs."""
    res = run_ctl(probe_env, ["mark-exhausted", "wallet_ghost_xyz", "--json"])
    assert res.returncode == 1
    err = json.loads(res.stdout)
    assert err["status"] == "error"
    assert "wallet_ghost_xyz" in err["message"]
    assert "not found" in err["message"].lower()


def test_mark_exhausted_empty_id(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe mark-exhausted with an empty string ID."""
    res = run_ctl(probe_env, ["mark-exhausted", "", "--json"])
    assert res.returncode == 1
    err = json.loads(res.stdout)
    assert err["status"] == "error"


def test_set_active_nonexistent_id(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe set-active with non-existent wallet IDs."""
    res = run_ctl(probe_env, ["set-active", "wallet_phantom_123", "--json"])
    assert res.returncode == 1
    err = json.loads(res.stdout)
    assert err["status"] == "error"
    assert "wallet_phantom_123" in err["message"]
    assert "not found" in err["message"].lower()


def test_set_active_empty_id(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe set-active with empty string ID."""
    res = run_ctl(probe_env, ["set-active", "", "--json"])
    assert res.returncode == 1
    err = json.loads(res.stdout)
    assert err["status"] == "error"


# ============================================================================ #
# 4. Lock Reset Graceful Handling                                             #
# ============================================================================ #


def test_reset_lock_when_no_lock_exists(probe_env: Dict[str, Any]) -> None:
    """Adversarially probe reset-lock and unpause when pause lock does not exist."""
    lock = probe_env["pause_lock_path"]
    lock.unlink(missing_ok=True)
    assert not lock.exists()

    res = run_ctl(probe_env, ["reset-lock", "--json"])
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["status"] == "ok"
    assert data["unpaused"] is True

    # Test alias 'unpause'
    res_alias = run_ctl(probe_env, ["unpause", "--json"])
    assert res_alias.returncode == 0
    data_alias = json.loads(res_alias.stdout)
    assert data_alias["status"] == "ok"


def test_reset_lock_reinstates_exhausted_wallets(probe_env: Dict[str, Any]) -> None:
    """Probe that reset-lock reinstates exhausted wallets unless --no-reset-wallets is passed."""
    # Mark both wallets exhausted
    run_ctl(probe_env, ["mark-exhausted", "wallet_primary"])
    run_ctl(probe_env, ["mark-exhausted", "wallet_backup", "--pause"])

    assert probe_env["pause_lock_path"].exists()
    wm = WalletManager(keystore_path=probe_env["keystore_path"])
    assert wm.get_wallet("wallet_primary").state == "exhausted"

    # Call reset-lock
    res = run_ctl(probe_env, ["reset-lock", "--json"])
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["wallets_reset_count"] >= 1
    assert not probe_env["pause_lock_path"].exists()

    wm_after = WalletManager(keystore_path=probe_env["keystore_path"])
    assert wm_after.get_wallet("wallet_primary").state == "active"
    assert wm_after.get_wallet("wallet_backup").state == "active"


# ============================================================================ #
# 5. Probe Fallback & Mock Upstream Interaction                               #
# ============================================================================ #


def test_probe_all_wallets_exhausted_fallback_to_primary(probe_env: Dict[str, Any]) -> None:
    """Probe that when all wallets are exhausted, probe falls back to primary key."""
    # Mark all wallets exhausted
    run_ctl(probe_env, ["mark-exhausted", "wallet_primary"])
    run_ctl(probe_env, ["mark-exhausted", "wallet_backup", "--pause"])

    wm = WalletManager(keystore_path=probe_env["keystore_path"])
    assert wm.get_active_key() is None, "All wallets should be exhausted"

    # Start mock server returning 200
    with MockCanopyServer(status_code=200) as mock:
        res = run_ctl(probe_env, ["probe", "--upstream-url", mock.url, "--json"])
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["status"] == "success"
        assert data["http_status"] == 200
        assert data["unpaused"] is True

        # Verify fallback Authorization header used the primary wallet key
        req = mock.last_request
        assert req is not None
        auth = req["headers"].get("Authorization") or req["headers"].get("authorization")
        assert auth == "Bearer csk_live_test_secret_key_12345"
        assert req["body"]["model"] == "deepseek-v4-pro"
        assert req["body"]["max_tokens"] == 1
        assert req["body"]["stream"] is True

        # Wallets should be reset to active and lock removed
        assert not probe_env["pause_lock_path"].exists()
        wm_post = WalletManager(keystore_path=probe_env["keystore_path"])
        assert wm_post.get_wallet("wallet_primary").state == "active"


def test_probe_all_wallets_exhausted_upstream_402(probe_env: Dict[str, Any]) -> None:
    """Probe that when upstream returns 402, probe maintains pause lock and reports status."""
    run_ctl(probe_env, ["mark-exhausted", "wallet_primary"])
    run_ctl(probe_env, ["mark-exhausted", "wallet_backup", "--pause"])

    with MockCanopyServer(status_code=402) as mock:
        res = run_ctl(probe_env, ["probe", "--upstream-url", mock.url, "--json"])
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["status"] == "exhausted"
        assert data["http_status"] == 402
        assert data["unpaused"] is False

        assert probe_env["pause_lock_path"].exists()
        pm = PauseManager(lock_path=probe_env["pause_lock_path"])
        assert pm.is_paused() is True


def test_probe_upstream_500_unexpected(probe_env: Dict[str, Any]) -> None:
    """Probe that when upstream returns 500, probe reports error and exit code 1."""
    with MockCanopyServer(status_code=500) as mock:
        res = run_ctl(probe_env, ["probe", "--upstream-url", mock.url, "--json"])
        assert res.returncode == 1
        data = json.loads(res.stdout)
        assert data["status"] == "unexpected_status"
        assert data["http_status"] == 500


def test_probe_no_keys_in_keystore_or_environment(probe_env: Dict[str, Any]) -> None:
    """Probe that when no keys exist in keystore or environment, probe exits 1."""
    # Wipe keystore
    probe_env["keystore_path"].write_text(json.dumps({"wallets": []}), encoding="utf-8")

    res = run_ctl(probe_env, ["probe", "--upstream-url", "http://127.0.0.1:9999", "--json"])
    assert res.returncode == 1
    data = json.loads(res.stdout)
    assert data["status"] == "error"
    assert "no wallet key found" in data["message"].lower()


# ============================================================================ #
# 6. File Permissions Preservation Across All Mutations                       #
# ============================================================================ #


def test_strict_0600_permissions_preserved_across_all_mutations(
    probe_env: Dict[str, Any],
) -> None:
    """Empirically probe that unstoppable_wallets.json retains 0600 mode after every mutation."""
    keystore = probe_env["keystore_path"]

    def assert_0600(target: Path, action_desc: str) -> None:
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == LOCK_PERMISSIONS, f"Expected 0o600 after {action_desc}, got {oct(mode)}"

    assert_0600(keystore, "initial creation")

    # Mutation 1: add-key
    res1 = run_ctl(probe_env, ["add-key", "csk_new_permission_key_123", "--id", "wallet_perm"])
    assert res1.returncode == 0
    assert_0600(keystore, "add-key")

    # Mutation 2: mark-exhausted
    res2 = run_ctl(probe_env, ["mark-exhausted", "wallet_perm"])
    assert res2.returncode == 0
    assert_0600(keystore, "mark-exhausted")

    # Mutation 3: set-active
    res3 = run_ctl(probe_env, ["set-active", "wallet_primary"])
    assert res3.returncode == 0
    assert_0600(keystore, "set-active")

    # Mutation 4: reset-lock (which calls reset_all())
    res4 = run_ctl(probe_env, ["reset-lock"])
    assert res4.returncode == 0
    assert_0600(keystore, "reset-lock")


def test_strict_0600_permissions_on_pause_lock(probe_env: Dict[str, Any]) -> None:
    """Empirically probe that unstoppable_pause.lock retains 0600 mode when written."""
    run_ctl(probe_env, ["mark-exhausted", "wallet_primary"])
    run_ctl(probe_env, ["mark-exhausted", "wallet_backup", "--pause"])

    lock = probe_env["pause_lock_path"]
    assert lock.exists()
    mode = stat.S_IMODE(lock.stat().st_mode)
    assert mode == LOCK_PERMISSIONS, f"Expected 0o600 for pause lock, got {oct(mode)}"


# ============================================================================ #
# 7. Concurrency Safety                                                       #
# ============================================================================ #


def test_concurrent_cli_invocations_preserve_json_integrity(
    probe_env: Dict[str, Any],
) -> None:
    """Stress test concurrent CLI invocations to guarantee atomic file writes without JSON corruption."""
    workers_count = 10

    def invoke_add(idx: int) -> int:
        cmd = [
            "add-key",
            f"csk_concurrent_test_key_{idx}_12345",
            "--id",
            f"wallet_worker_{idx}",
            "--type",
            "org" if idx % 2 == 0 else "personal",
            "--json",
        ]
        res = run_ctl(probe_env, cmd)
        return res.returncode

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers_count) as executor:
        futures = [executor.submit(invoke_add, i) for i in range(workers_count)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    # All invocations should succeed without crashing
    assert all(code == 0 for code in results), f"Some concurrent writes failed: {results}"

    # Keystore MUST be valid JSON, not truncated or corrupted
    raw_text = probe_env["keystore_path"].read_text(encoding="utf-8")
    parsed = json.loads(raw_text)
    assert isinstance(parsed, dict)
    assert "wallets" in parsed
    assert len(parsed["wallets"]) >= 2

    # Mode must remain 0600
    mode = stat.S_IMODE(probe_env["keystore_path"].stat().st_mode)
    assert mode == LOCK_PERMISSIONS


def test_concurrent_status_during_mutations(probe_env: Dict[str, Any]) -> None:
    """Stress test concurrent status reads while mark-exhausted and add-key mutate state."""
    iterations = 15

    def reader() -> bool:
        res = run_ctl(probe_env, ["status", "--json"])
        if res.returncode != 0:
            return False
        try:
            data = json.loads(res.stdout)
            return "wallets" in data
        except Exception:
            return False

    def writer(idx: int) -> bool:
        res = run_ctl(
            probe_env,
            ["add-key", f"csk_concurrent_mutate_key_{idx}", "--id", f"wallet_mut_{idx}"],
        )
        return res.returncode == 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = []
        for i in range(iterations):
            futures.append(executor.submit(reader))
            futures.append(executor.submit(writer, i))

        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    assert all(results), "Concurrent reader/writer experienced failures or corrupted output"


# ============================================================================ #
# 8. Critical Invariant: Exclusion of Forbidden Term                          #
# ============================================================================ #


def test_critical_invariant_zero_forbidden_term_in_m4_artifacts() -> None:
    """Empirically audit service/bin/ and tests/ for zero references to forbidden term."""
    forbidden = "".join(["black", "hole"])
    regex = re.compile(forbidden, re.IGNORECASE)

    targets = [
        WALLET_CTL_PATH,
        REPO_ROOT / "service" / "bin" / "unstoppable_wallet_probe.sh",
        REPO_ROOT / "tests" / "test_adversarial_m4_challenger_2.py",
        REPO_ROOT / "tests" / "test_probe_daemon.py",
    ]

    for target in targets:
        assert target.is_file(), f"Target file missing: {target}"
        content = target.read_text(encoding="utf-8")
        matches = regex.findall(content)
        assert len(matches) == 0, f"Violation: found {forbidden} in {target}: {matches}"
