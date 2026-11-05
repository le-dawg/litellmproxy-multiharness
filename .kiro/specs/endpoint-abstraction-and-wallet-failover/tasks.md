# Implementation Tasks: endpoint-abstraction-and-wallet-failover

## Phase 1: Classification & Hardening (P0)
- [x] 1.1 Create `service/agentic/endpoint_abstraction/canopy_classifier.py` implementing 3-tier matching (status 402, Canopy JSON body, masked 500 regex) _Boundary: `service/agentic/endpoint_abstraction/canopy_classifier.py`_
- [x] 1.2 Update `FAILOVER_STATUS_CODES` and `EXHAUSTION_PHRASES` in `tool_choice_guard.py` _Boundary: `tool_choice_guard.py`_
- [x] 1.3 Add unit tests in `tests/test_canopy_classifier.py` covering mock 402, `MaskedHTTPStatusError`, and `APIConnectionError` _Boundary: `tests/test_canopy_classifier.py`_

## Phase 2: Multi-Wallet Keystore & State Persistence (P1)
- [x] 2.1 Implement `WalletManager` in `service/agentic/endpoint_abstraction/wallet_manager.py` with atomic JSON persistence and mtime reload _Boundary: `service/agentic/endpoint_abstraction/wallet_manager.py`_
- [x] 2.2 Implement multi-wallet rotation logic (rotate on exhaustion, cooldown recovery) _Boundary: `service/agentic/endpoint_abstraction/wallet_manager.py`_
- [x] 2.3 Add unit tests in `tests/test_wallet_manager.py` verifying state rotation, persistence, and mtime invalidation _Boundary: `tests/test_wallet_manager.py`_

## Phase 3: Fast-Fail Guard, Error Enrichment & Hook Integration (P1)
- [x] 3.1 Implement `PauseManager` and `ErrorEnricher` in `service/agentic/endpoint_abstraction/` _Boundary: `service/agentic/endpoint_abstraction/`_
- [x] 3.2 Refactor `tool_choice_guard.py` to remove hardcoded single-key clobbering across all 3 hooks and integrate `WalletManager` and `PauseManager` _Boundary: `tool_choice_guard.py`_
- [x] 3.3 Add router fallbacks for `deepseek-v4-pro` and `glm-5.3-flash` in `config/config.yaml` to `antigravity-pro` _Boundary: `config/config.yaml`_

## Phase 4: Hourly Background Curl Probe Daemon & CLI Controller (P2)
- [x] 4.1 Implement `service/bin/unstoppable_wallet_probe.sh` with PID locking, `stream: true` curl payload, and `osascript` notification _Boundary: `service/bin/unstoppable_wallet_probe.sh`_
- [x] 4.2 Implement CLI controller `service/bin/wallet_ctl.py` to inspect wallet pool, force probe, and reset pause lock _Boundary: `service/bin/wallet_ctl.py`_
- [x] 4.3 Add test in `tests/test_probe_daemon.py` verifying probe execution and lock clearance _Boundary: `tests/test_probe_daemon.py`_

## Phase 5: Verification & End-to-End Replay (P0 Gate)
- [x] 5.1 Replay historical 402 session failure against proxy verifying clean failover to Wallet 2 _Boundary: `tests/test_canopy_replay.py`_
- [x] 5.2 Verify that all-wallets-exhausted state produces an enhanced 402 response and activates probe daemon _Boundary: `tests/test_canopy_replay.py`_
- [x] 5.3 Run full test suite via `uv run pytest tests/` confirming 100% pass rate _Boundary: `tests/`_
