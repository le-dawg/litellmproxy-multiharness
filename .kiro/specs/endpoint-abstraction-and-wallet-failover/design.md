# Technical Design: endpoint-abstraction-and-wallet-failover

## Module Architecture
Create modular components under `service/agentic/endpoint_abstraction/`:

```
service/agentic/endpoint_abstraction/
├── __init__.py
├── wallet_manager.py       # Keystore, state persistence, mtime dynamic cache
├── canopy_classifier.py    # Multi-tier deterministic 402 & string classifier
├── pause_manager.py        # Lockfile lifecycle and fast-fail guard
└── error_enricher.py       # Diagnostic error payload formatter
```

### Integration Points in `tool_choice_guard.py`
1. **`async_pre_call_hook`**:
   - Call `PauseManager.check_pause()`. If locked, raise enriched `fastapi.HTTPException(status_code=402)`.
2. **`async_pre_call_deployment_hook`**:
   - Fetch active key via `WalletManager.get_active_key()`.
   - Inject authorization header and `stream: true` for Unstoppable endpoints without clobbering model group structures.
3. **`async_post_call_failure_hook`**:
   - Inspect failure via `CanopyClassifier.is_canopy_exhaustion(original_exception)`.
   - If True:
     - Mark wallet exhausted: `WalletManager.mark_exhausted(current_wallet_id)`.
     - If all exhausted:
       - `PauseManager.set_paused()`.
       - `PauseManager.ensure_probe_daemon_running()`.
       - Return `ErrorEnricher.format_pause_exception()`.

### Probe Script (`service/bin/unstoppable_wallet_probe.sh`)
- Uses `flock` or PID check on `~/.litellm/unstoppable_probe.pid`.
- Enforces loop interval of 3600 seconds.
- Uses `stream: true` in curl request to avoid upstream 400 Bad Request.
