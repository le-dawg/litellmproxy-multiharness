# Requirements: endpoint-abstraction-and-wallet-failover

## 1. Functional Requirements

### R1: Multi-Tier Deterministic Canopy 402 Classification
- The classifier MUST intercept upstream failures from `https://app.unstoppable.ai/api/v1/llm-proxy/anthropic`.
- Must match across three layers:
  1. Status layer: `status_code in (402, "402")` or `code in (402, "402")`.
  2. Payload layer: JSON inspection of `response.text` for `canopy_proxy: "CANOPY_AI_CREDITS_PROXY"`, `reason: "personal_wallet_empty"`, `code: "no_billing_source"`.
  3. Masked layer: Regex on `APIConnectionError` / string representations matching `\b402\b`, `\bpayment[_\s]required\b`, or `\bai credits are unavailable\b`.

### R2: Multi-Wallet Keystore with Dynamic State Synchronization
- Support an arbitrary pool of wallets configured in `~/.litellm/unstoppable_wallets.json` and env vars (`UNSTOPPABLE_WALLET_KEYS`).
- Wallet schema: `{"id": str, "key": str, "type": "personal"|"org", "state": "active"|"exhausted"|"cooldown", "last_exhausted": float|null}`.
- Check file modification time (`mtime`) on incoming requests to dynamically sync updates written by external probe scripts without proxy restarts.

### R3: In-Flight Deployment Rotation
- Remove hardcoded token clobbering from `async_pre_call_hook`, `async_pre_request_hook`, and `async_pre_call_deployment_hook`.
- In `async_pre_call_deployment_hook`, dynamically resolve credentials using `WalletManager.get_active_wallet()`.
- On Canopy 402 failure: mark current wallet `EXHAUSTED` and switch active wallet pointer to the next healthy entry.

### R4: Actionable Error Enhancement
- When all wallets are exhausted, `async_post_call_failure_hook` returns a `fastapi.HTTPException(status_code=402)` with an enriched diagnostic payload:
  - Clear title: `[Unstoppable AI Resource Depleted]`
  - Detailed reason and Canopy correlation ID.
  - Clear notice that work is paused and an hourly probe is monitoring funds.
  - Exact CLI command to resume (`claude --resume`).

### R5: Background Hourly Curl Probe Daemon & Notification
- When all wallets become exhausted, spawn `service/bin/unstoppable_wallet_probe.sh` protected by PID file (`~/.litellm/unstoppable_probe.pid`).
- Probe sends:
  `curl -s -m 15 https://app.unstoppable.ai/api/v1/llm-proxy/anthropic/v1/messages -H "Authorization: Bearer $KEY" -H "anthropic-version: 2023-06-01" -d '{"model":"deepseek-v4-pro","max_tokens":1,"stream":true,"messages":[{"role":"user","content":"probe"}]}'`
- On HTTP 200:
  - Updates `~/.litellm/unstoppable_wallets.json` setting wallet to `active`.
  - Removes `~/.litellm/unstoppable_pause.lock`.
  - Emits desktop alert: `osascript -e 'display notification "Unstoppable AI credits replenished. MultiHarness unpaused." with title "LiteLLM MultiHarness"'`.
  - Exits cleanly.

### R6: Fast-Fail Backpressure Guard
- If `~/.litellm/unstoppable_pause.lock` is present, `async_pre_call_hook` fast-fails immediately with HTTP 402, preventing wasteful upstream calls or client retry thrashing.
