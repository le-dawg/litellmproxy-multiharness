# Product Steering: LiteLLM MultiHarness

## Vision & Purpose
`litellmproxy-multiharness` is a resilient, low-latency local AI proxy and endpoint bridge engineered for local developer agents and harnesses (Claude Code, OpenAI Codex, Antigravity CLI, OpenWork). It aggregates disparate local and remote model runtimes behind a unified, high-reliability endpoint.

## Core Problem
Complex agentic workflows running context-heavy multi-turn tasks (150k+ tokens/turn) frequently exhaust provider credit quotas, hit rate limits, or encounter transient upstream gateway failures (e.g. Canopy 402, HTTP 502/504). Unhandled failures crash agent sessions, corrupt task state, and force manual debugging.

## Strategic Capabilities
1. **Zero-Crash Failover**: Seamlessly transition from primary local models to remote fallbacks without dropping in-flight agent sessions.
2. **Deterministic Upstream Error Classification**: Detect credit exhaustion, billing faults, and gateway errors across both raw HTTP and masked framework exceptions.
3. **Multi-Wallet Pooling & Key Rotation**: Transparently rotate credentials across an arbitrary pool of API keys when individual wallets deplete.
4. **Agent-Aware Backpressure & Work Pausing**: Intercept complete resource exhaustion to pause downstream clients cleanly, avoid burning retry tokens, and alert operators with actionable recovery paths.
5. **Autonomous Health & Billing Probing**: Run background verification daemons that monitor replenishment and restore normal traffic without human intervention.
