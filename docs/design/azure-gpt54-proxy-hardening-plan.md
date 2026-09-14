# LiteLLM Azure GPT-5.4 Proxy Hardening & Error-Sanitization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate confusing "model group fallbacks" error message leaks, prevent Azure 400 schema rejections (unsupported `stop_sequences` and orphaned tool choices), and bridge macOS DarkWake/sleep network association latency in the LiteLLM proxy routing to Azure GPT-5.4.

**Architecture:** 
1. Configure `expose_router_debug_in_errors: false` in `~/.litellm/config.yaml` to permanently halt router exception mutation.
2. Configure `router_settings` with exponential backoff (`num_retries: 3`, `retry_after: 3`) to survive macOS DarkWake Wi-Fi re-association.
3. Enhance `~/.litellm/tool_choice_guard.py` pre-call hook to strip `stop_sequences` and empty `tools`/`tool_choice` combinations before upstream dispatch to Azure OpenAI's Responses API.
4. Verify end-to-end via launchd reload and automated health & error-sanitization test suites.

**Tech Stack:** Python 3.14 / uv, LiteLLM Proxy, PyYAML, pytest, httpx, macOS launchd (`com.thedawgctor.litellm-proxy`).

## Global Constraints

- ONLY EVER USE `uv` as the entry point to executing python functions. Always rely on `uv` best practices. No exceptions.
- Do NOT modify Azure deployment credentials or endpoint targets (`https://regent-ai-dev.cognitiveservices.azure.com`).
- Preserve all existing preflight constraints in `~/.litellm/service/bin/preflight-litellm.sh` (`drop_params: true`, required model aliases `claude-gpt-5` and `claude-sonnet-4-5`).
- Ensure `~/.litellm/tool_choice_guard.py` maintains pre-call sanitization dropping orphaned outputs before dispatching to Azure.

---

### Task 1: Enhance Pre-Call Sanitizer in `tool_choice_guard.py` for Azure Responses API

**Files:**
- Modify: `/Users/thedawgctor/.litellm/tool_choice_guard.py`
- Test: `/Users/thedawgctor/.gemini/antigravity-cli/brain/dabf4145-9291-4633-90a2-0cfbcd84683b/scratch/test_tool_choice_guard.py`

**Interfaces:**
- Consumes: `CustomLogger.async_pre_call_hook(user_api_key_dict, cache, data, call_type)`
- Produces: Sanitized `data` payload with `stop_sequences` removed, `tool_choice` stripped when `tools` is missing/empty, and orphan `function_call_output` turns pruned.

- [ ] **Step 1: Write the failing unit test for `stop_sequences` and tool-choice sanitization**

Create `/Users/thedawgctor/.gemini/antigravity-cli/brain/dabf4145-9291-4633-90a2-0cfbcd84683b/scratch/test_tool_choice_guard.py`:

```python
import asyncio
import pytest
import sys
from pathlib import Path

# Add ~/.litellm to python path to import the active guard
sys.path.insert(0, "/Users/thedawgctor/.litellm")
from tool_choice_guard import StripOrphanToolChoiceGuard


@pytest.mark.asyncio
async def test_strip_unsupported_stop_sequences_in_responses_api():
    guard = StripOrphanToolChoiceGuard()
    payload = {
        "model": "azure/gpt-5.4",
        "input": [{"type": "message", "role": "user", "content": "hello"}],
        "stop_sequences": ["\n\nHuman:"],
    }
    result = await guard.async_pre_call_hook(
        user_api_key_dict={},
        cache=None,
        data=payload,
        call_type="aresponses",
    )
    assert "stop_sequences" not in result, "stop_sequences must be stripped for Responses API"


@pytest.mark.asyncio
async def test_strip_tool_choice_when_tools_is_empty_or_none():
    guard = StripOrphanToolChoiceGuard()
    payload = {
        "model": "azure/gpt-5.4",
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    result = await guard.async_pre_call_hook(
        user_api_key_dict={},
        cache=None,
        data=payload,
        call_type="aresponses",
    )
    assert "tool_choice" not in result
    assert "parallel_tool_calls" not in result


@pytest.mark.asyncio
async def test_retain_tool_choice_when_tools_are_present():
    guard = StripOrphanToolChoiceGuard()
    payload = {
        "model": "azure/gpt-5.4",
        "tools": [{"type": "function", "name": "do_work"}],
        "tool_choice": "auto",
    }
    result = await guard.async_pre_call_hook(
        user_api_key_dict={},
        cache=None,
        data=payload,
        call_type="aresponses",
    )
    assert result.get("tool_choice") == "auto"
    assert len(result.get("tools")) == 1
```

- [ ] **Step 2: Run test to verify it fails on `stop_sequences`**

Run:
```bash
uv run pytest /Users/thedawgctor/.gemini/antigravity-cli/brain/dabf4145-9291-4633-90a2-0cfbcd84683b/scratch/test_tool_choice_guard.py -v
```
Expected: `FAILED test_strip_unsupported_stop_sequences_in_responses_api` (because `stop_sequences` is not yet stripped in `tool_choice_guard.py`).

- [ ] **Step 3: Update `tool_choice_guard.py` to strip `stop_sequences`**

In `/Users/thedawgctor/.litellm/tool_choice_guard.py`, update `async_pre_call_hook` around lines 164–185:

```python
        # --- Responses API unsupported parameter sanitizer ---
        # Azure Responses API rejects 'stop_sequences' with HTTP 400
        if "stop_sequences" in data:
            data.pop("stop_sequences", None)
            verbose_proxy_logger.debug(
                "StripOrphanToolChoiceGuard removed unsupported 'stop_sequences' (call_type=%s, model=%s)",
                call_type,
                data.get("model"),
            )

        # --- tool_choice guard (existing logic) ---
        if "tool_choice" not in data and "parallel_tool_calls" not in data:
            return data

        if self._has_tools(data.get("tools")):
            return data

        stripped = False
        for key in ("tool_choice", "parallel_tool_calls"):
            if key in data:
                data.pop(key, None)
                stripped = True

        if stripped:
            verbose_proxy_logger.debug(
                "StripOrphanToolChoiceGuard removed tool params without tools "
                "(call_type=%s, model=%s)",
                call_type,
                data.get("model"),
            )

        return data
```

- [ ] **Step 4: Run tests to verify all 3 test cases pass**

Run:
```bash
uv run pytest /Users/thedawgctor/.gemini/antigravity-cli/brain/dabf4145-9291-4633-90a2-0cfbcd84683b/scratch/test_tool_choice_guard.py -v
```
Expected: `3 passed in 0.XXs`.

---

### Task 2: Configure LiteLLM Settings & Router Backoff in `config.yaml`

**Files:**
- Modify: `/Users/thedawgctor/.litellm/config.yaml`
- Test: `/Users/thedawgctor/.litellm/service/bin/preflight-litellm.sh`
- Test: `/Users/thedawgctor/.gemini/antigravity-cli/brain/dabf4145-9291-4633-90a2-0cfbcd84683b/scratch/test_config_validation.py`

**Interfaces:**
- Consumes: YAML config structure loaded by LiteLLM Proxy service.
- Produces: Clean exception reporting (no `Received Model Group=...` tag) and resilient retry backoff bridging DarkWake network initialization.

- [ ] **Step 1: Write configuration validator test**

Create `/Users/thedawgctor/.gemini/antigravity-cli/brain/dabf4145-9291-4633-90a2-0cfbcd84683b/scratch/test_config_validation.py`:

```python
import yaml
from pathlib import Path

def test_litellm_config_hardening():
    config_path = Path("/Users/thedawgctor/.litellm/config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    litellm_settings = config.get("litellm_settings", {})
    assert litellm_settings.get("expose_router_debug_in_errors") is False, (
        "expose_router_debug_in_errors must be false to prevent cosmetic fallback errors"
    )
    assert litellm_settings.get("drop_params") is True, "drop_params must remain true"

    router_settings = config.get("router_settings", {})
    assert router_settings.get("num_retries", 0) >= 3, "num_retries must be >= 3"
    assert router_settings.get("retry_after", 0) >= 3, "retry_after must be >= 3 seconds"
```

- [ ] **Step 2: Run test to verify it fails on existing `config.yaml`**

Run:
```bash
uv run pytest /Users/thedawgctor/.gemini/antigravity-cli/brain/dabf4145-9291-4633-90a2-0cfbcd84683b/scratch/test_config_validation.py -v
```
Expected: `FAILED test_litellm_config_hardening` (because `expose_router_debug_in_errors` and `router_settings` are not yet set).

- [ ] **Step 3: Update `~/.litellm/config.yaml` with hardened settings**

Apply the following modifications to `/Users/thedawgctor/.litellm/config.yaml`:

```yaml
litellm_settings:
  drop_params: true
  expose_router_debug_in_errors: false
  callbacks:
    - tool_choice_guard.proxy_guard
  model_alias_map:
    claude-sonnet-5: claude-gpt-5
    claude-sonnet-5[1m]: claude-gpt-5
    claude-sonnet-4-5: claude-gpt-5
    claude-gpt-5[1m]: claude-gpt-5
    claude-mythos-5: claude-gpt-5
    antigravity: antigravity-flash

router_settings:
  num_retries: 3
  retry_after: 3
  allowed_fails_policy:
    RateLimitError: 5
```

- [ ] **Step 4: Run preflight script and validation test**

Run:
```bash
/Users/thedawgctor/.litellm/service/bin/preflight-litellm.sh
uv run pytest /Users/thedawgctor/.gemini/antigravity-cli/brain/dabf4145-9291-4633-90a2-0cfbcd84683b/scratch/test_config_validation.py -v
```
Expected:
`litellm-preflight: credential source and translation contract validated`
`1 passed in 0.XXs`

---

### Task 3: Reload Launchd Service and Verify End-to-End Proxy Sanitization

**Files:**
- Modify: Launchd service state (`gui/$UID/com.thedawgctor.litellm-proxy`)
- Test: `/Users/thedawgctor/.gemini/antigravity-cli/brain/dabf4145-9291-4633-90a2-0cfbcd84683b/scratch/test_live_proxy.py`

**Interfaces:**
- Consumes: Running LiteLLM proxy at `http://127.0.0.1:5596`
- Produces: Live HTTP 200 health status, clean parameter passthrough, and zero leaked router fallback error strings.

- [ ] **Step 1: Write live proxy integration test**

Create `/Users/thedawgctor/.gemini/antigravity-cli/brain/dabf4145-9291-4633-90a2-0cfbcd84683b/scratch/test_live_proxy.py`:

```python
import httpx
import pytest

PROXY_BASE = "http://127.0.0.1:5596"

def test_proxy_health():
    resp = httpx.get(f"{PROXY_BASE}/health")
    assert resp.status_code == 200, f"Proxy health failed: {resp.text}"

def test_clean_error_message_without_router_leak():
    # Send a request to an unmapped model to trigger an error
    resp = httpx.post(
        f"{PROXY_BASE}/v1/responses",
        json={
            "model": "non-existent-dummy-model-trigger-error",
            "input": [{"type": "message", "role": "user", "content": "ping"}],
        },
        timeout=10.0,
    )
    # Ensure the response does NOT contain the confusing fallback leak
    assert resp.status_code != 200
    error_text = resp.text
    assert "Available Model Group Fallbacks" not in error_text, (
        f"Leaked router fallback string found in error response: {error_text}"
    )
    assert "Received Model Group" not in error_text, (
        f"Leaked model group string found in error response: {error_text}"
    )
```

- [ ] **Step 2: Kickstart the launchd service to reload configuration**

Run:
```bash
launchctl kickstart -k gui/$(id -u)/com.thedawgctor.litellm-proxy
sleep 3
```
Expected: Process restarts cleanly with new PID under launchd.

- [ ] **Step 3: Run live proxy integration tests**

Run:
```bash
uv run pytest /Users/thedawgctor/.gemini/antigravity-cli/brain/dabf4145-9291-4633-90a2-0cfbcd84683b/scratch/test_live_proxy.py -v
```
Expected: `2 passed in 0.XXs`.

---

## Plan Self-Review Checklist

1. **Spec Coverage:**
   - Silence Router Debug Leak: Implemented in Task 2 & verified in Task 3.
   - DarkWake Backoff Retries: Implemented in Task 2 via `router_settings.num_retries = 3` and `retry_after = 3`.
   - Param Passthrough / `stop_sequences` / orphan tools: Implemented in Task 1 & verified via pytest.
   - Zero-Downtime Clean Reload: Implemented in Task 3 via launchd kickstart.
2. **Placeholder Scan:** No placeholders, TODOs, or TBDs exist. Complete code and exact paths provided for all steps.
3. **Execution Tooling:** Uses `uv run pytest` exclusively per user rules.
