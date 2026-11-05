# AGENTS.md — LiteLLM MultiHarness Unified Engineering & Agent Rules

## 1. CRITICAL INVARIANT: GitHub Remote Hygiene (Local-Only Specs & Agent Artifacts)

> **MANDATORY RULE FOR ALL AGENTS & OPERATORS:**
> Internal specification directories and agent orchestration artifacts are committed to **local development/reconciliation branches ONLY** (such as `feature/unified-multiharness-reconciliation`) and **MUST NEVER BE PUSHED TO GITHUB REMOTE (`origin`)**.

### Protected Local-Only Paths (NEVER Push to `origin` / GitHub)
1. **`.kiro/`** — Kiro specifications (`.kiro/specs/endpoint-abstraction-and-wallet-failover/`) and steering documents (`.kiro/steering/`).
2. **`.agents/`** — Multi-agent teamwork briefings, dispatches, reviews, and handoff logs (`.agents/teamwork/`).
3. **Local Scratch / Session Artifacts** — Any temporary planning or local machine state files.

### Mandatory Pre-Push Checklist Before Pushing to GitHub (`origin`)
Before executing `git push origin ...` or opening/updating any GitHub Pull Request:
1. Create or switch to a clean outbound release branch if needed.
2. Ensure `.kiro/` and `.agents/` are stripped from the index on the outbound branch (`git rm -r --cached .kiro .agents`) and listed in `.gitignore` prior to pushing to `origin`.
3. Verify via `git status` and `git diff origin/main --name-only | grep -E "^(\.kiro|\.agents)/"` that **zero** `.kiro/` or `.agents/` files exist in the outbound diff.

---

## 2. Unified Repository Architecture (MBP + Mac mini Reconciled State)

This branch (`feature/unified-multiharness-reconciliation`) unifies the divergent histories of **MacBook Pro (`mbp` / `dawgctors-den`)** and **Mac mini (`thes-Mac-mini`)**:

### A. Endpoint Abstraction Layer & Multi-Wallet Failover (`service/agentic/endpoint_abstraction/`)
- **`canopy_classifier.py`**: 3-tier deterministic classifier for Upstream Canopy Proxy HTTP 402 (`personal_wallet_empty`, `no_billing_source`, masked 500 `APIConnectionError`) with benign number false-positive rejection.
- **`wallet_manager.py`**: Thread-safe multi-wallet keystore (`~/.litellm/unstoppable_wallets.json`, mode `0600`) with `stat_signature` (`mtime` + inode) dynamic disk invalidation, in-flight credential rotation, and cooldown recovery.
- **`pause_manager.py`**: Atomic lockfile lifecycle controller (`~/.litellm/unstoppable_pause.lock`) providing sub-millisecond fast-fail HTTP 402 backpressure and background probe daemon PID supervision.
- **`error_enricher.py`**: Synthesizes Interface Contract 3 HTTP 402 diagnostic payloads with correlation ID extraction and actionable resume instructions (`claude --resume`).

### B. Background Probing & Operator CLI (`service/bin/`)
- **`unstoppable_wallet_probe.sh`**: Hourly background curl probe daemon (`"stream": true`) that automatically unpauses the proxy, reactivates replenished wallets, and dispatches macOS desktop notifications (`osascript`).
- **`wallet_ctl.py`**: Operator CLI for inspecting wallet pool health (`status`), adding keys (`add-key`), triggering probes (`probe`), and clearing pause locks (`reset-lock`).

### C. Antigravity Bridge & Circuit Breaker Guard (`service/bin/antigravity_bridge.py`, `tool_choice_guard.py`)
- **Headless LS Connection Persistence**: Eliminates `open -a Antigravity.app` GUI window spawning on headless/daemon invocations and caches healthy Language Server connections for 300s.
- **499 Client Cancellation Suppression**: Distinguishes client-initiated stream cancellations (`HTTP 499` / `context canceled`) from upstream gateway failures (`502` / `504`) so cancelled turns never trip the failover circuit breaker.
- **Remote Unreachable Auto-Recovery**: Automatically resets `AntigravityCircuitBreaker` back to primary if the remote ZeroTier fallback node (`10.147.17.130:8788`) is unreachable (`connection refused` / `no route to host`).
- **In-Bridge Tool Emulation & Prompt Normalization**: Structured `<tool_call>` demarcation injection, SSE streaming delta synthesis, and trailing hook normalization.

### D. Architectural & Design Specifications on This Branch
- **`.kiro/specs/endpoint-abstraction-and-wallet-failover/`**: Complete Kiro specification (`brief.md`, `requirements.md`, `design.md`, `tasks.md`, `spec.json`).
- **`docs/design/remote-antigravity-agentic-loop-spec.md`**: Full specification for canonical protocol-correct tool-calling across OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages over local and remote Antigravity bridges.

---

## 3. Execution & Tooling Standards
- **Python Entrypoint**: ONLY EVER USE `uv` (`uv run python ...`, `uv run pytest`). Never invoke system `python` or `pytest` directly.
- **Runtime Sync**: Deployed proxy files live in `~/.litellm/config.yaml` and `~/.litellm/tool_choice_guard.py`. Keep them synchronized with the repository when testing live proxy behavior on `:5596`.
