# Roadmap: LiteLLM MultiHarness Hardening

## Overview
Transform `litellmproxy-multiharness` into an agent-resilient routing and failover gateway. We address the root cause of recent session crashes (upstream Canopy AI credits 402 rejections on Unstoppable AI) by introducing a modular Endpoint Abstraction Layer (EAL), multi-wallet pooling, fast-fail pause guards, and background curl health probes.

## Approach Decision
- **Chosen**: Two-Tier Endpoint Abstraction Layer (EAL) combining LiteLLM Router declarative fallback groups with a dynamic `WalletManager` and multi-tier `CanopyClassifier` in `tool_choice_guard.py`.
- **Why**: Decouples model identity from credential selection, catches both raw HTTP 402 and LiteLLM-masked HTTP 500 exceptions, synchronizes state across daemon and background probes via file `mtime`, and pauses agent turns cleanly without burning retry tokens.
- **Rejected Alternatives**:
  - *Hardcoded in-hook rotation*: Rejected due to high coupling and clobbering across multiple hooks.
  - *Portkey/One-API replacement*: Rejected due to heavy external server/database dependencies conflicting with a lightweight local developer macOS setup.

## Scope
- **In**:
  - 3-tier Canopy 402 error classification (status 402, JSON payload, 500 masked regex).
  - Multi-wallet keystore with atomic disk persistence (`~/.litellm/unstoppable_wallets.json`).
  - Fast-fail pause guard (`~/.litellm/unstoppable_pause.lock`).
  - Actionable diagnostic error enrichment for downstream harnesses.
  - Background hourly curl probe daemon (`service/bin/unstoppable_wallet_probe.sh`).
  - Declarative router fallbacks in `config/config.yaml`.
- **Out**:
  - Direct billing or credit purchase automation.
  - Upstream Canopy proxy protocol changes.

## Constraints
- Python execution strictly via `uv run`.
- Hourly curl probes must use `"stream": true` to satisfy upstream gateway requirements.
- Info-agent-LAT5 directory must remain pristine.

## Boundary Strategy
- **Why this split**: Keeps error classification purely functional and testable; isolates state persistence to a dedicated manager; maintains LiteLLM integration strictly as a thin adapter layer.
- **Shared seams to watch**: Interaction between `async_pre_call_hook` fast-fail pause check and client timeout policies.

## Specs (dependency order)
- [ ] endpoint-abstraction-and-wallet-failover -- Deterministic Canopy 402 classification, multi-wallet rotation, pause lock, and hourly curl probe daemon. Dependencies: none
