"""Empirical Adversarial Test Suite for Milestone 3 (Fast-Fail, Error Enrichment, Hooks & Router Fallback).

Conducted by: teamwork_challenger_m3_2
Target Components:
- service.agentic.endpoint_abstraction.pause_manager.PauseManager
- service.agentic.endpoint_abstraction.error_enricher.ErrorEnricher
- service.agentic.endpoint_abstraction.error_enricher.CanopyResourceDepletedError
- service.agentic.endpoint_abstraction.wallet_manager.WalletManager
- tool_choice_guard.StripOrphanToolChoiceGuard
- config/config.yaml router_settings.fallbacks

Directives verified empirically:
1. Fast-fail 402 in deployment hook when pause lock is active (with exact Contract 3 fields).
2. Dynamic active key injection across hooks (no hardcoded/stale key clobbering, immediate rotation visibility).
3. In-flight wallet failover in async_post_call_failure_hook on simulated Canopy 402 errors.
4. Pause lock trigger when all wallets in pool are exhausted.
5. Mandatory "stream": True preservation under all conditions for Unstoppable endpoints.
6. Router fallbacks configuration mapping deepseek-v4-pro and glm-5.3-flash to antigravity-pro.
7. Critical Invariant: Zero references to the forbidden term in code, config, or tests.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import pytest
import yaml

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from service.agentic.endpoint_abstraction.error_enricher import (
    DEFAULT_DEPLETED_MESSAGE,
    DEFAULT_RESUME_INSTRUCTION,
    CanopyResourceDepletedError,
    ErrorDetailDict,
    ErrorEnricher,
)
from service.agentic.endpoint_abstraction.pause_manager import PauseManager, PauseState
from service.agentic.endpoint_abstraction.wallet_manager import WalletManager
from tool_choice_guard import StripOrphanToolChoiceGuard, is_unstoppable_model


# ============================================================================ #
# 1. Fast-Fail 402 in Deployment Hook Probes                                  #
# ============================================================================ #

@pytest.mark.asyncio
async def test_adversarial_deployment_hook_fast_fail_contract_compliance(tmp_path: Path):
    """Empirically probe that pre_call_deployment_hook raises exact Contract 3 402 payload when paused."""
    lock_file = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_file)
    pm.pause(
        reason="All wallets exhausted (personal_wallet_empty)",
        correlation_id="req_canopy_adv_fail_9988",
    )
    assert pm.is_paused() is True

    guard = StripOrphanToolChoiceGuard(pause_manager=pm)

    # Test all Unstoppable models
    test_models = [
        {"model": "deepseek-v4-pro", "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic"},
        {"model": "glm-5.3-flash", "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic"},
        {"model": "claude-opus-5.5", "api_base": None},
        {"model": "claude-sonnet-5.5", "api_base": None},
        {"model": "custom-proxy-model", "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic"},
    ]

    for model_kwargs in test_models:
        with pytest.raises(Exception) as exc_info:
            await guard.async_pre_call_deployment_hook(dict(model_kwargs))

        exc = exc_info.value
        # Check HTTP status 402
        assert getattr(exc, "status_code", None) == 402

        # Check detail dictionary
        detail = getattr(exc, "detail", None)
        assert isinstance(detail, dict), f"Expected dict detail, got {type(detail)}"

        # Dual compatibility check: flat or nested
        assert "error" in detail
        err_body = detail["error"]

        # Exact contract fields
        assert err_body["code"] == 402
        assert err_body["type"] == "payment_required"
        assert "[Unstoppable AI Resource Depleted]" in err_body["message"]
        assert err_body["canopy_correlation_id"] == "req_canopy_adv_fail_9988"
        assert err_body["status"] == "paused"
        assert "claude --resume" in err_body["resume_instruction"]

        # If CanopyResourceDepletedError, verify response headers
        if isinstance(exc, CanopyResourceDepletedError):
            assert exc.headers.get("X-Canopy-Correlation-Id") == "req_canopy_adv_fail_9988"
            assert "application/json" in exc.headers.get("Content-Type", "")


@pytest.mark.asyncio
async def test_adversarial_deployment_hook_allows_local_and_remote_during_pause(tmp_path: Path):
    """Empirically probe that active pause lock ONLY restricts Unstoppable endpoints, not local models."""
    lock_file = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_file)
    pm.pause(reason="Depleted credits", correlation_id="req_canopy_block_other")

    guard = StripOrphanToolChoiceGuard(pause_manager=pm)

    unaffected_deployments = [
        {"model": "antigravity-flash-low", "api_base": "http://127.0.0.1:8788/v1"},
        {"model": "antigravity-flash-medium", "api_base": "http://127.0.0.1:8788/v1"},
        {"model": "antigravity-pro", "api_base": "http://127.0.0.1:8788/v1"},
        {"model": "antigravity-remote-flash-low", "api_base": "http://10.0.0.1:8788/v1"},
        {"model": "gemini-3.8-flash-high", "api_base": "http://127.0.0.1:8788/v1"},
    ]

    for req in unaffected_deployments:
        res = await guard.async_pre_call_deployment_hook(dict(req))
        assert res is not None
        assert res["model"] == req["model"]


@pytest.mark.asyncio
async def test_adversarial_dynamic_unpause_recovers_without_restart(tmp_path: Path):
    """Empirically probe that removing the pause lock instantly unpauses subsequent requests."""
    lock_file = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_file)
    pm.pause(reason="Depleted credits", correlation_id="req_canopy_transient")

    guard = StripOrphanToolChoiceGuard(pause_manager=pm)
    req = {
        "model": "deepseek-v4-pro",
        "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic",
    }

    # 1. First call fails fast
    with pytest.raises(Exception) as exc_info:
        await guard.async_pre_call_deployment_hook(dict(req))
    assert getattr(exc_info.value, "status_code", None) == 402

    # 2. External unpause (e.g. background probe recovers or user resets)
    pm.unpause(reason="Replenished")
    assert pm.is_paused() is False

    # 3. Next call succeeds without server restart
    res = await guard.async_pre_call_deployment_hook(dict(req))
    assert res is not None
    assert res["model"] == "anthropic/deepseek-v4-pro"


# ============================================================================ #
# 2. Active Key Injection & Mandatory Stream Preservation                     #
# ============================================================================ #

@pytest.mark.asyncio
async def test_adversarial_active_key_injection_across_all_hooks(tmp_path: Path):
    """Empirically probe that active healthy key is injected across all 3 hooks."""
    keystore = tmp_path / "unstoppable_wallets.json"
    wm = WalletManager(keystore_path=keystore, auto_create=True)
    wm.add_wallet("csk_primary_active_key_001", wallet_id="w_primary", set_active=True)
    wm.add_wallet("csk_secondary_standby_002", wallet_id="w_secondary")

    guard = StripOrphanToolChoiceGuard(wallet_manager=wm)

    # Hook 1: async_pre_call_hook
    pre_call_data = {"model": "claude-opus-5.5", "stream": False}
    pre_call_res = await guard.async_pre_call_hook(
        user_api_key_dict={}, cache=None, data=pre_call_data, call_type="acompletion"
    )
    assert pre_call_res["model"] == "deepseek-v4-pro"
    assert pre_call_res["api_key"] == "csk_primary_active_key_001"
    assert pre_call_res["extra_headers"]["Authorization"] == "Bearer csk_primary_active_key_001"
    assert pre_call_res["stream"] is True  # stream: False overridden to True!

    # Hook 2: async_pre_request_hook
    req_kwargs = {
        "model": "deepseek-v4-pro",
        "extra_headers": {"Authorization": "Bearer csk_stale_clobbered"},
        "stream": False,
    }
    pre_req_res = await guard.async_pre_request_hook(
        model="deepseek-v4-pro",
        messages=[],
        request_kwargs=req_kwargs,
    )
    assert pre_req_res["extra_headers"]["Authorization"] == "Bearer csk_primary_active_key_001"
    assert pre_req_res["stream"] is True

    # Hook 3: async_pre_call_deployment_hook
    dep_kwargs = {
        "model": "deepseek-v4-pro",
        "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic",
        "extra_headers": {},
        "stream": None,
    }
    dep_res = await guard.async_pre_call_deployment_hook(dep_kwargs)
    assert dep_res["api_key"] == "csk_primary_active_key_001"
    assert dep_res["extra_headers"]["Authorization"] == "Bearer csk_primary_active_key_001"
    assert dep_res["stream"] is True

    # Now switch active wallet to w_secondary
    wm.set_active_wallet("w_secondary")
    assert wm.active_wallet_id == "w_secondary"
    assert wm.get_active_key() == "csk_secondary_standby_002"

    # Hook 3 immediately uses the newly active key
    dep_kwargs_2 = {
        "model": "deepseek-v4-pro",
        "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic",
    }
    dep_res_2 = await guard.async_pre_call_deployment_hook(dep_kwargs_2)
    assert dep_res_2["api_key"] == "csk_secondary_standby_002"
    assert dep_res_2["extra_headers"]["Authorization"] == "Bearer csk_secondary_standby_002"


@pytest.mark.asyncio
async def test_adversarial_stream_true_enforced_strictly(tmp_path: Path):
    """Empirically probe that stream: True is strictly enforced for Unstoppable endpoints."""
    keystore = tmp_path / "unstoppable_wallets.json"
    wm = WalletManager(keystore_path=keystore, auto_create=True)
    wm.add_wallet("csk_key_stream_test", wallet_id="w_stream", set_active=True)

    guard = StripOrphanToolChoiceGuard(wallet_manager=wm)

    for stream_val in [False, None, 0, "", "false"]:
        dep_kwargs = {
            "model": "deepseek-v4-pro",
            "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic",
            "stream": stream_val,
        }
        res = await guard.async_pre_call_deployment_hook(dep_kwargs)
        assert res["stream"] is True, f"Failed for input stream={stream_val!r}"

    # Verify non-unstoppable model is NOT coerced to stream: True
    local_kwargs = {
        "model": "antigravity-flash",
        "api_base": "http://127.0.0.1:8788/v1",
        "stream": False,
    }
    local_res = await guard.async_pre_call_deployment_hook(local_kwargs)
    assert local_res["stream"] is False


# ============================================================================ #
# 3. In-Flight Wallet Failover & Exhaustion Cascade                           #
# ============================================================================ #

@pytest.mark.asyncio
async def test_adversarial_in_flight_multi_wallet_failover_cascade(tmp_path: Path):
    """Empirically probe 3-wallet failover cascade to exhaustion and pause lock trigger."""
    keystore = tmp_path / "unstoppable_wallets.json"
    lock_file = tmp_path / "unstoppable_pause.lock"

    wm = WalletManager(keystore_path=keystore, auto_create=True)
    wm.add_wallet("csk_wallet_1_alpha", wallet_id="w1", set_active=True)
    wm.add_wallet("csk_wallet_2_bravo", wallet_id="w2")
    wm.add_wallet("csk_wallet_3_charlie", wallet_id="w3")

    pm = PauseManager(lock_path=lock_file)
    assert pm.is_paused() is False

    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    # Initial state: w1 is active
    assert wm.active_wallet_id == "w1"
    assert guard._resolve_unstoppable_key() == "csk_wallet_1_alpha"

    # --- Step 1: Upstream 402 on w1 ---
    resp1 = httpx.Response(
        status_code=402,
        json={"error": {"reason": "personal_wallet_empty"}},
        headers={"x-canopy-correlation-id": "req_canopy_cascade_01"},
        request=httpx.Request("POST", "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic/v1/messages"),
    )
    exc1 = httpx.HTTPStatusError("402 Payment Required", request=resp1.request, response=resp1)

    await guard.async_post_call_failure_hook(
        request_data={"model": "deepseek-v4-pro", "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic"},
        original_exception=exc1,
        user_api_key_dict={},
    )

    # w1 is exhausted; rotated to w2; not paused yet!
    assert wm.active_wallet_id == "w2"
    assert guard._resolve_unstoppable_key() == "csk_wallet_2_bravo"
    assert pm.is_paused() is False

    # --- Step 2: Masked connection error on w2 ---
    class MaskedAPIConnectionError(Exception):
        pass

    exc2 = MaskedAPIConnectionError(
        "APIConnectionError: 402 Payment Required: personal_wallet_empty [req_canopy_cascade_02]"
    )

    await guard.async_post_call_failure_hook(
        request_data={"model": "glm-5.3-flash", "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic"},
        original_exception=exc2,
        user_api_key_dict={},
    )

    # w2 is exhausted; rotated to w3; not paused yet!
    assert wm.active_wallet_id == "w3"
    assert guard._resolve_unstoppable_key() == "csk_wallet_3_charlie"
    assert pm.is_paused() is False

    # --- Step 3: Final 402 on w3 (all wallets exhausted!) ---
    resp3 = httpx.Response(
        status_code=402,
        json={"error": {"reason": "personal_wallet_empty"}},
        headers={"x-canopy-correlation-id": "req_canopy_final_cascade_03"},
        request=httpx.Request("POST", "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic/v1/messages"),
    )
    exc3 = httpx.HTTPStatusError("402 Payment Required", request=resp3.request, response=resp3)

    await guard.async_post_call_failure_hook(
        request_data={"model": "deepseek-v4-pro", "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic"},
        original_exception=exc3,
        user_api_key_dict={},
    )

    # All wallets exhausted!
    assert wm.has_active_wallet() is False
    assert pm.is_paused() is True

    # Lockfile created with correlation id from step 3
    pause_state = pm.get_pause_state()
    assert pause_state is not None
    assert pause_state.correlation_id == "req_canopy_final_cascade_03"
    assert "All wallets exhausted" in pause_state.reason

    # Subsequent pre-call deployment fast-fails immediately with 402
    with pytest.raises(Exception) as exc_info:
        await guard.async_pre_call_deployment_hook({
            "model": "deepseek-v4-pro",
            "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic",
        })
    fast_fail_exc = exc_info.value
    assert getattr(fast_fail_exc, "status_code", None) == 402
    assert fast_fail_exc.detail["error"]["canopy_correlation_id"] == "req_canopy_final_cascade_03"


@pytest.mark.asyncio
async def test_adversarial_deployment_hook_handles_depleted_keystore_on_entry(tmp_path: Path, monkeypatch):
    """Empirically probe that entering deployment hook with depleted keystore immediately triggers pause."""
    keystore = tmp_path / "unstoppable_wallets.json"
    lock_file = tmp_path / "unstoppable_pause.lock"

    # Ensure get_unstoppable_code_key does not return fallback files
    monkeypatch.setattr("tool_choice_guard.get_unstoppable_code_key", lambda: None)

    # Create empty keystore
    keystore.write_text(json.dumps({"wallets": [], "active_wallet_id": None}))
    wm = WalletManager(keystore_path=keystore)
    pm = PauseManager(lock_path=lock_file)
    assert pm.is_paused() is False

    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    with pytest.raises(Exception) as exc_info:
        await guard.async_pre_call_deployment_hook({
            "model": "deepseek-v4-pro",
            "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic",
        })

    assert getattr(exc_info.value, "status_code", None) == 402
    assert pm.is_paused() is True
    assert "All wallets exhausted" in pm.get_pause_state().reason


# ============================================================================ #
# 4. Router Fallbacks to antigravity-pro                                      #
# ============================================================================ #

def test_adversarial_router_fallbacks_in_config_and_router_instantiation():
    """Empirically probe that config.yaml and ~/.litellm/config.yaml map Unstoppable models to antigravity-pro."""
    config_paths = [
        REPO_ROOT / "config" / "config.yaml",
        Path.home() / ".litellm" / "config.yaml",
    ]

    for p in config_paths:
        assert p.is_file(), f"Config file not found: {p}"
        with open(p, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        fallbacks = cfg.get("router_settings", {}).get("fallbacks", [])
        fallback_map = {}
        for entry in fallbacks:
            if isinstance(entry, dict):
                fallback_map.update(entry)

        # deepseek-v4-pro -> antigravity-pro
        assert "deepseek-v4-pro" in fallback_map, f"Missing deepseek-v4-pro fallback in {p}"
        assert fallback_map["deepseek-v4-pro"] == ["antigravity-pro"]

        # glm-5.3-flash -> antigravity-pro
        assert "glm-5.3-flash" in fallback_map, f"Missing glm-5.3-flash fallback in {p}"
        assert fallback_map["glm-5.3-flash"] == ["antigravity-pro"]

        # Verify antigravity-pro deployment is present in model_list
        models_in_list = {m["model_name"] for m in cfg.get("model_list", []) if "model_name" in m}
        assert "antigravity-pro" in models_in_list, f"antigravity-pro not declared in model_list in {p}"

    # Verify instantiation with litellm.Router
    try:
        from litellm import Router

        router = Router(
            model_list=cfg["model_list"],
            fallbacks=cfg["router_settings"]["fallbacks"],
        )
        assert router.fallbacks is not None
        assert {"deepseek-v4-pro": ["antigravity-pro"]} in router.fallbacks
        assert {"glm-5.3-flash": ["antigravity-pro"]} in router.fallbacks
    except ImportError:
        pass


# ============================================================================ #
# 5. ErrorEnricher Contract & Edge Cases                                      #
# ============================================================================ #

def test_adversarial_error_enricher_edge_cases_and_serialization():
    """Empirically probe ErrorEnricher and ErrorDetailDict under adversarial conditions."""
    # 1. ErrorDetailDict dual-compatibility
    raw_payload = ErrorEnricher.build_payload(correlation_id="req_canopy_test_dual_01")
    detail = ErrorDetailDict(raw_payload["error"])

    # Flat lookup
    assert detail["code"] == 402
    assert detail["type"] == "payment_required"
    assert detail["canopy_correlation_id"] == "req_canopy_test_dual_01"

    # Nested lookup
    assert "error" in detail
    assert detail["error"]["code"] == 402
    assert detail["error"]["canopy_correlation_id"] == "req_canopy_test_dual_01"

    # Standard json serialization without cycles or TypeError
    json_str = json.dumps(detail)
    parsed = json.loads(json_str)
    assert parsed["code"] == 402
    assert parsed["canopy_correlation_id"] == "req_canopy_test_dual_01"

    # 2. Extract correlation ID from weird/adversarial sources
    assert ErrorEnricher.extract_correlation_id(source="", fallback=False) is None
    assert ErrorEnricher.extract_correlation_id(source=12345, fallback=False) is None
    assert ErrorEnricher.extract_correlation_id(source=[], fallback=False) is None

    # Deeply nested dict with non-standard capitalization
    deep_source = {
        "metadata": {
            "headers": {
                "X-CaNoPy-CoRrElAtIoN-Id": "req_canopy_nested_mixed_case"
            }
        }
    }
    extracted = ErrorEnricher.extract_correlation_id(source=deep_source)
    assert extracted == "req_canopy_nested_mixed_case"

    # Malformed JSON string source
    corrupt_str = "{bad_json: 123"
    assert ErrorEnricher.extract_correlation_id(source=corrupt_str, fallback=False) == corrupt_str


# ============================================================================ #
# 6. Critical Invariant: Exclusion of Forbidden Term                          #
# ============================================================================ #

def test_adversarial_critical_invariant_zero_forbidden_term():
    """Empirically audit the entire repo and active litellm files for forbidden terms."""
    forbidden = "".join(["black", "hole"])
    regex = re.compile(forbidden, re.IGNORECASE)

    targets_to_scan = [
        REPO_ROOT / "service" / "agentic" / "endpoint_abstraction" / "error_enricher.py",
        REPO_ROOT / "service" / "agentic" / "endpoint_abstraction" / "pause_manager.py",
        REPO_ROOT / "service" / "agentic" / "endpoint_abstraction" / "wallet_manager.py",
        REPO_ROOT / "service" / "agentic" / "endpoint_abstraction" / "canopy_classifier.py",
        REPO_ROOT / "service" / "agentic" / "endpoint_abstraction" / "__init__.py",
        REPO_ROOT / "tool_choice_guard.py",
        REPO_ROOT / "config" / "config.yaml",
        REPO_ROOT / "tests" / "test_error_enricher.py",
        REPO_ROOT / "tests" / "test_pause_manager.py",
        REPO_ROOT / "tests" / "test_tool_choice_guard.py",
        Path.home() / ".litellm" / "tool_choice_guard.py",
        Path.home() / ".litellm" / "config.yaml",
    ]

    for target in targets_to_scan:
        assert target.is_file(), f"Target file does not exist: {target}"
        text = target.read_text(encoding="utf-8")
        matches = regex.findall(text)
        assert len(matches) == 0, f"Found forbidden term in {target}: {matches}"
