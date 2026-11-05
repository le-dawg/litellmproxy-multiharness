# Brief: endpoint-abstraction-and-wallet-failover

## Problem
Intensive agentic sessions (e.g. Claude Code in `~/Desktop/IWI-LAT/info-agent-LAT5`) consuming context-heavy payloads (~155k tokens/turn) deplete prepaid Unstoppable AI credits. When upstream Canopy Proxy returns HTTP 402 (`personal_wallet_empty`), LiteLLM MultiHarness fails to classify the error, fails to rotate to backup wallets, and passes a masked error downstream, abruptly terminating user workflows.

## Current State
- `tool_choice_guard.py` hardcodes single-token resolution in 3 separate hooks (`async_pre_call_hook`, `async_pre_request_hook`, `async_pre_call_deployment_hook`).
- `FAILOVER_STATUS_CODES` only checks `(429, 502, 504)`. Status code 402 is omitted.
- LiteLLM exception mapping converts Anthropic 402 errors to generic 500 `APIConnectionError`.
- No multi-wallet keystore, no atomic state persistence, no error enhancement, and no recovery probe daemon.

## Desired Outcome
1. Deterministic detection of HTTP 402 and Canopy proxy exhaustion signatures across both raw and masked exceptions.
2. Enhanced, actionable diagnostic messages delivered downstream.
3. Automatic rotation across an $N$-wallet keystore (`~/.litellm/unstoppable_wallets.json`).
4. If all wallets are exhausted, work is cleanly paused via `unstoppable_pause.lock`, and an hourly curl probe daemon (`stream: true`) is spawned with automatic macOS desktop notification on recovery.

## Approach
Implement a modular Endpoint Abstraction Layer (EAL) in `service/agentic/endpoint_abstraction/` with:
- Multi-tier error classification (`canopy_classifier.py`).
- Keystore with `mtime` file-sync invalidation (`wallet_manager.py`).
- Fast-fail backpressure pause manager (`pause_manager.py`).
- Enriched error diagnostic formatter (`error_enricher.py`).
- Detached PID-locked background hourly curl probe daemon (`service/bin/unstoppable_wallet_probe.sh`).

## Scope
- **In**:
  - `service/agentic/endpoint_abstraction/wallet_manager.py`
  - `service/agentic/endpoint_abstraction/canopy_classifier.py`
  - `service/agentic/endpoint_abstraction/pause_manager.py`
  - `service/agentic/endpoint_abstraction/error_enricher.py`
  - `service/bin/unstoppable_wallet_probe.sh`
  - Integration into `tool_choice_guard.py` and `config/config.yaml`
  - Comprehensive unit and replay tests in `tests/`
- **Out**:
  - Direct billing or credit payment card processing.
  - Upstream Canopy proxy modifications.
  - Client binary modifications to Claude Code or Codex.

## Boundary Candidates
- Seam 1: Error classifier logic vs LiteLLM exception handler.
- Seam 2: Wallet keystore persistence vs live hook invocation.
- Seam 3: Pause lockfile state vs background probe runner.

## Out of Boundary
- Downstream client terminal rendering logic.
- Credit replenishment payment gateway APIs.

## Upstream / Downstream
- **Upstream**: Unstoppable AI Canopy Proxy (`app.unstoppable.ai/api/v1/llm-proxy/anthropic`).
- **Downstream**: Claude Code, Codex Desktop, Antigravity CLI.

## Constraints
- Python execution strictly via `uv run`.
- Background curl probes MUST specify `"stream": true` to satisfy upstream Canopy gateway requirements.
- Strictly zero contamination of `/Users/thedawgctor/Desktop/IWI-LAT/info-agent-LAT5`.
