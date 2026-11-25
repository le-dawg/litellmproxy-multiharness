# Dispatch for Challenger 2

You are Challenger 2 conducting adversarial stress testing on litellmproxy-multiharness.
Your working directory: /Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/teamwork_preview_challenger_2
Project root: /Users/thedawgctor/Desktop/litellmproxy-multiharness
Original Request: /Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/ORIGINAL_REQUEST.md
Project Document: /Users/thedawgctor/Desktop/litellmproxy-multiharness/PROJECT.md

MANDATORY USER RULES:
- ONLY EVER USE `uv` as the entry point to executing python functions. Always rely on `uv` best practices. No exceptions.
- Maintain your progress.md with "Last visited: [timestamp]" updates.

YOUR MISSION:
Adversarially stress-test error handling, circuit breaker failover, and prompt flattening:
- Generate stress tests for:
  * Repeated upstream empty responses (asserting 502 Bad Gateway is consistently returned).
  * Socket timeouts and abrupt connection drops (asserting 504 and 502 status codes).
  * Rapid sequential requests while circuit breaker is in `STATE_FAILOVER` vs `STATE_PROBING`.
  * Multi-turn conversations ending with multiple layered system hooks (`UserPromptSubmit`, `Plan mode`, custom hooks) to test whether prompt normalization always enforces explicit `USER:` invocation.
  * Historical session replay with injected latency and synthetic error drops.
- Execute stress-test scripts via `uv run python`.
- Confirm whether failover and recovery behavior are robust.
- Deliver self-contained `handoff.md` with your findings, code snippets, test results, and verdict.
- Send completion message via send_message.
