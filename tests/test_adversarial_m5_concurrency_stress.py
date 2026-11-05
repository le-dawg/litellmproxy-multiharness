"""Milestone 5 Iteration 2 Adversarial Concurrency Stress Probing Suite.

Empirical verification of:
1. High concurrency wallet churn (30-50 concurrent tasks) with randomized arrival delays.
2. Idempotency guard strictly stopping rotation at w2 when failing on w1.
3. Cascading multi-stage concurrent churn across full pool.
4. Simultaneous cross-wallet concurrent failure reports.
5. Unknown token and malformed header graceful fallback without crashes.

Verification Command:
    cd /Users/thedawgctor/Desktop/dawgctor-personal-tools/litellmproxy-multiharness
    uv run pytest tests/test_adversarial_m5_concurrency_stress.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from service.agentic.endpoint_abstraction.canopy_classifier import CanopyClassifier
from service.agentic.endpoint_abstraction.error_enricher import ErrorEnricher
from service.agentic.endpoint_abstraction.pause_manager import PauseManager
from service.agentic.endpoint_abstraction.wallet_manager import Wallet, WalletManager
from tool_choice_guard import StripOrphanToolChoiceGuard


@pytest.fixture
def isolated_env(tmp_path: Path) -> Tuple[Path, Path]:
    litellm_dir = tmp_path / ".litellm"
    litellm_dir.mkdir(parents=True, exist_ok=True)
    wallets_path = litellm_dir / "unstoppable_wallets.json"
    lock_path = litellm_dir / "unstoppable_pause.lock"
    return wallets_path, lock_path


def _make_402_exception(correlation_id: str = "req_stress_402", reason: str = "personal_wallet_empty") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic/v1/messages")
    resp_402 = httpx.Response(
        status_code=402,
        headers={"x-canopy-correlation-id": correlation_id},
        json={"error": {"reason": reason, "code": "no_billing_source"}},
        request=req,
    )
    return httpx.HTTPStatusError("402 Payment Required", request=req, response=resp_402)


# ============================================================================ #
# 1. 30 Concurrent Tasks with Randomized Arrival Delays                        #
# ============================================================================ #

@pytest.mark.asyncio
async def test_stress_30_concurrent_wallet_churn_with_arrival_jitter(isolated_env: Tuple[Path, Path]):
    """Stress-test 30 concurrent failure reports for w1 with arrival delays up to 30ms.

    Must rotate strictly to w2 and stop there; w2 and w3 must remain active.
    """
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_stress_fail_1", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_stress_healthy_2", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w3", "key": "csk_stress_healthy_3", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)

    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    exc = _make_402_exception("req_stress_30")
    request_data = {
        "model": "deepseek-v4-pro",
        "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic",
        "api_key": "csk_stress_fail_1",
        "extra_headers": {"Authorization": "Bearer csk_stress_fail_1"},
    }

    async def report_with_jitter(idx: int):
        jitter = random.uniform(0.0005, 0.030)
        await asyncio.sleep(jitter)
        await guard.async_post_call_failure_hook(request_data, exc, {})

    # Launch 30 concurrent tasks
    await asyncio.gather(*(report_with_jitter(i) for i in range(30)))

    # Verifications
    assert wm.active_wallet_id == "w2"
    assert pm.is_paused() is False

    w1 = wm.get_wallet("w1")
    w2 = wm.get_wallet("w2")
    w3 = wm.get_wallet("w3")
    assert w1 is not None and w1.state == "exhausted"
    assert w2 is not None and w2.state == "active"
    assert w3 is not None and w3.state == "active"

    # Disk state verification
    disk_data = json.loads(wallets_path.read_text(encoding="utf-8"))
    assert disk_data["active_wallet_id"] == "w2"
    disk_w1 = next(w for w in disk_data["wallets"] if w["id"] == "w1")
    disk_w2 = next(w for w in disk_data["wallets"] if w["id"] == "w2")
    disk_w3 = next(w for w in disk_data["wallets"] if w["id"] == "w3")
    assert disk_w1["state"] == "exhausted"
    assert disk_w2["state"] == "active"
    assert disk_w3["state"] == "active"


# ============================================================================ #
# 2. 50 Concurrent Tasks with Wide Jitter (High Pressure)                      #
# ============================================================================ #

@pytest.mark.asyncio
async def test_stress_50_concurrent_wallet_churn_with_wide_jitter(isolated_env: Tuple[Path, Path]):
    """Stress-test 50 concurrent tasks with arrival jitter up to 50ms.

    Proves high-throughput contention maintains idempotency and atomic persistence.
    """
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_fail_p1_50", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_healthy_p2_50", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w3", "key": "csk_healthy_org_50", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)

    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    exc = _make_402_exception("req_stress_50")
    request_data = {
        "model": "deepseek-v4-pro",
        "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic",
        "api_key": "csk_fail_p1_50",
    }

    async def report(idx: int):
        await asyncio.sleep(random.uniform(0.001, 0.050))
        await guard.async_post_call_failure_hook(request_data, exc, {})

    await asyncio.gather(*(report(i) for i in range(50)))

    assert wm.active_wallet_id == "w2"
    assert pm.is_paused() is False
    assert wm.get_wallet("w1").state == "exhausted"
    assert wm.get_wallet("w2").state == "active"
    assert wm.get_wallet("w3").state == "active"


# ============================================================================ #
# 3. Cascading Multi-Stage 30-Concurrency Churn (w1 -> w2 -> w3 -> pause)     #
# ============================================================================ #

@pytest.mark.asyncio
async def test_stress_cascading_30_concurrency_across_full_pool(isolated_env: Tuple[Path, Path]):
    """Three sequential waves of 30 concurrent failures across 3 wallets.

    Wave 1: 30 failures on w1 -> rotates to w2, stops at w2.
    Wave 2: 30 failures on w2 -> rotates to w3, stops at w3.
    Wave 3: 30 failures on w3 -> all exhausted -> triggers PauseManager lock.
    """
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_cascade_1", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_cascade_2", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w3", "key": "csk_cascade_3", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)

    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    exc = _make_402_exception("req_cascade")

    # --- Wave 1: 30 tasks on w1 ---
    req1 = {"model": "deepseek-v4-pro", "api_key": "csk_cascade_1"}
    await asyncio.gather(*(
        asyncio.create_task((lambda: asyncio.sleep(random.uniform(0.0005, 0.015)))()).then(
            lambda _: guard.async_post_call_failure_hook(req1, exc, {})
        ) if False else (
            # direct async helper
            (lambda: _run_with_delay(guard, req1, exc, random.uniform(0.0005, 0.015)))()
        )
        for _ in range(30)
    ))

    assert wm.active_wallet_id == "w2"
    assert pm.is_paused() is False
    assert wm.get_wallet("w1").state == "exhausted"
    assert wm.get_wallet("w2").state == "active"
    assert wm.get_wallet("w3").state == "active"

    # --- Wave 2: 30 tasks on w2 ---
    req2 = {"model": "deepseek-v4-pro", "api_key": "csk_cascade_2"}
    await asyncio.gather(*(_run_with_delay(guard, req2, exc, random.uniform(0.0005, 0.015)) for _ in range(30)))

    assert wm.active_wallet_id == "w3"
    assert pm.is_paused() is False
    assert wm.get_wallet("w1").state == "exhausted"
    assert wm.get_wallet("w2").state == "exhausted"
    assert wm.get_wallet("w3").state == "active"

    # --- Wave 3: 30 tasks on w3 ---
    req3 = {"model": "deepseek-v4-pro", "api_key": "csk_cascade_3"}
    await asyncio.gather(*(_run_with_delay(guard, req3, exc, random.uniform(0.0005, 0.015)) for _ in range(30)))

    assert pm.is_paused() is True
    assert wm.has_active_wallet() is False
    assert wm.get_wallet("w3").state == "exhausted"


async def _run_with_delay(guard: StripOrphanToolChoiceGuard, req_data: dict, exc: Exception, delay: float):
    await asyncio.sleep(delay)
    await guard.async_post_call_failure_hook(req_data, exc, {})


# ============================================================================ #
# 4. Simultaneous Cross-Wallet Failures (15 on w1 and 15 on w2 concurrently)   #
# ============================================================================ #

@pytest.mark.asyncio
async def test_stress_simultaneous_cross_wallet_failures(isolated_env: Tuple[Path, Path]):
    """In-flight requests that used w1 and w2 both experience 402 failures simultaneously.

    Both w1 and w2 must be marked exhausted, and the active pointer must rotate to w3.
    """
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_simul_1", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_simul_2", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w3", "key": "csk_simul_3", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)

    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    exc = _make_402_exception("req_simul")
    req1 = {"model": "deepseek-v4-pro", "api_key": "csk_simul_1"}
    req2 = {"model": "deepseek-v4-pro", "api_key": "csk_simul_2"}

    tasks = []
    for _ in range(15):
        tasks.append(_run_with_delay(guard, req1, exc, random.uniform(0.001, 0.020)))
        tasks.append(_run_with_delay(guard, req2, exc, random.uniform(0.001, 0.020)))

    await asyncio.gather(*tasks)

    assert wm.active_wallet_id == "w3"
    assert pm.is_paused() is False
    assert wm.get_wallet("w1").state == "exhausted"
    assert wm.get_wallet("w2").state == "exhausted"
    assert wm.get_wallet("w3").state == "active"


# ============================================================================ #
# 5. Unknown Token Handling & Graceful Fallback                                #
# ============================================================================ #

@pytest.mark.asyncio
async def test_unknown_tokens_in_failure_hook_graceful_fallback(isolated_env: Tuple[Path, Path]):
    """Requests carrying unknown tokens must gracefully fall back to active wallet without crash."""
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_known_1", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_known_2", "type": "personal", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)

    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)
    exc = _make_402_exception("req_unknown_token")

    # 1. Unknown token in request_data["api_key"]
    req_unknown_1 = {
        "model": "deepseek-v4-pro",
        "api_key": "csk_completely_unknown_token_9999",
    }
    # Should not raise exception
    await guard.async_post_call_failure_hook(req_unknown_1, exc, {})
    # Fell back to active_wallet_id (w1) and rotated to w2
    assert wm.active_wallet_id == "w2"
    assert wm.get_wallet("w1").state == "exhausted"

    # 2. Unknown token in Authorization header
    req_unknown_2 = {
        "model": "deepseek-v4-pro",
        "extra_headers": {"Authorization": "Bearer csk_unrecognized_bearer_token"},
    }
    await guard.async_post_call_failure_hook(req_unknown_2, exc, {})
    # Fell back to active_wallet_id (w2) -> all exhausted
    assert pm.is_paused() is True
    assert wm.get_wallet("w2").state == "exhausted"


@pytest.mark.asyncio
async def test_malformed_and_missing_credentials_in_failure_hook(isolated_env: Tuple[Path, Path]):
    """Requests with None, empty, or non-dict payloads must gracefully fall back without crash."""
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_known_1", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_known_2", "type": "personal", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)

    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)
    exc = _make_402_exception("req_malformed")

    # Missing api_key and empty headers in request_data with unstoppable model
    req_no_key = {
        "model": "deepseek-v4-pro",
        "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic",
    }
    await guard.async_post_call_failure_hook(req_no_key, exc, {})
    assert wm.active_wallet_id == "w2"

    # Malformed headers (non-string Authorization)
    req_malformed = {
        "model": "deepseek-v4-pro",
        "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic",
        "api_key": None,
        "extra_headers": {"Authorization": 12345},  # Non-string header value
    }
    await guard.async_post_call_failure_hook(req_malformed, exc, {})
    assert pm.is_paused() is True


@pytest.mark.asyncio
async def test_unknown_token_when_all_wallets_already_exhausted(isolated_env: Tuple[Path, Path]):
    """When pool is fully exhausted, failure report with unknown token triggers pause without crash."""
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_ex_1", "type": "personal", "state": "exhausted", "last_exhausted": time.time()},
        ],
        "active_wallet_id": None,
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)

    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)
    exc = _make_402_exception("req_all_exhausted")

    req_data = {"model": "deepseek-v4-pro", "api_key": "csk_ghost_unknown"}
    await guard.async_post_call_failure_hook(req_data, exc, {})

    assert pm.is_paused() is True


@pytest.mark.asyncio
async def test_pre_call_and_deployment_hooks_inject_active_token(isolated_env: Tuple[Path, Path]):
    """Incoming request carrying an unknown token gets dynamically replaced with active healthy key."""
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_primary_active_key", "type": "personal", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)

    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    # 1. async_pre_call_hook with Unstoppable Opus model
    data_payload: Dict[str, Any] = {
        "model": "claude-opus-5-5-20251101",
        "api_key": "csk_unknown_client_token",
        "messages": [{"role": "user", "content": "hello"}],
    }
    res_data = await guard.async_pre_call_hook(None, None, data_payload, "completion")
    assert res_data is not None
    assert res_data["model"] == "deepseek-v4-pro"
    assert res_data["api_key"] == "csk_primary_active_key"
    assert res_data["extra_headers"]["Authorization"] == "Bearer csk_primary_active_key"
    assert res_data["stream"] is True

    # 2. async_pre_call_deployment_hook with Unstoppable deepseek model
    call_kwargs: Dict[str, Any] = {
        "model": "deepseek-v4-pro",
        "api_key": "csk_unknown_client_token",
        "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic",
        "extra_headers": {"Authorization": "Bearer csk_unknown_client_token"},
    }
    await guard.async_pre_call_deployment_hook(call_kwargs)
    assert call_kwargs["api_key"] == "csk_primary_active_key"
    assert call_kwargs["extra_headers"]["Authorization"] == "Bearer csk_primary_active_key"
    assert call_kwargs["stream"] is True


def test_wallet_manager_get_wallet_by_key_edge_cases(isolated_env: Tuple[Path, Path]):
    """Direct testing of get_wallet_by_key and mark_exhausted boundary conditions."""
    wallets_path, _ = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_key_alpha", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_key_beta", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)

    wm = WalletManager(keystore_path=wallets_path)

    # Unknown key -> None
    assert wm.get_wallet_by_key("csk_nonexistent") is None
    # Empty string -> None
    assert wm.get_wallet_by_key("") is None
    # None -> None (str(None) == "None")
    assert wm.get_wallet_by_key(None) is None  # type: ignore[arg-type]
    # Key with leading/trailing whitespace matches clean key
    w = wm.get_wallet_by_key("   csk_key_alpha   ")
    assert w is not None and w.id == "w1"

    # mark_exhausted with unknown wallet ID
    res = wm.mark_exhausted("ghost_wallet_id")
    # Returns active wallet without modifying state
    assert res is not None and res.id == "w1"
    assert wm.get_wallet("w1").state == "active"
