# Project: litellmproxy-multiharness Hardening

## Architecture
- **Language Server Bridge (`service/bin/antigravity_bridge.py`)**:
  HTTP server (default port 8788) bridging OpenAI `/v1/chat/completions` API calls to upstream Antigravity Language Server via protobuf RPC (`GetModelResponse`). Emulates structured JSON tool calling, sanitizes prompt flattening, normalizes trailing hook blocks, and enforces fail-fast HTTP 502/504 errors on empty upstream responses or timeouts.
- **Circuit Breaker & Fallback Guard (`tool_choice_guard.py`)**:
  LiteLLM custom guard pre/post call hooks and circuit breaker (`AntigravityCircuitBreaker`). Intercepts 502/504 upstream failures and quota exhaustion to transition to `STATE_FAILOVER`, immediately routing requests to remote fallback deployment (`antigravity-remote-flash-medium`).
- **LiteLLM Router & Multi-harness Config (`config/config.yaml`)**:
  Manages model definitions, port configurations (port 5596), fallbacks, and retry policies.
- **Automated Replay Test Harness (`tests/`)**:
  Ephemeral test harness and pytest test suites running via `uv run python -m pytest`. Replays historical Claude Code sessions (e.g. `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93`) in isolated environments using dynamic ephemeral ports (`127.0.0.1:0`).

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| F1 | In-Bridge Tool Schema Injection | Inject OpenAI tool definitions (`payload.get("tools")`) into system prompt with explicit structured JSON instructions and demarcation markers (`<tool_call>...`). | M1 | Survey 1 (R1) |
| F2 | Model Response Demarcation Parser | Parse model text responses for `<tool_call>{"name": "...", "arguments": {...}}</tool_call>` demarcation blocks in both pure tool and mixed conversational turns. | M1 | Survey 1 (R1) |
| F3 | Dual-Mode Response Synthesizer | Synthesize standard OpenAI format: `choices[0].message.tool_calls` for non-streaming, and segregated SSE chunks (`content`, `tool_calls`, `finish_reason: "tool_calls"`) for streaming. | M1 | Survey 1 (R1) |
| F4 | Multi-Turn Tool History Preservation | Retain previous assistant `tool_calls` and tool role outputs during prompt flattening, ensuring models retain context across multi-turn tool sessions. | M1 | Survey 1 (R1) |
| F5 | Upstream Empty Response Fail-Fast (502) | Return HTTP 502 Bad Gateway immediately when `GetModelResponse` returns empty `{}` or whitespace, preventing silent 0-token 200 OK completions. | M1 | Survey 2 (R2) |
| F6 | Upstream Timeout & Disconnect Mapping (504/502) | Map socket/urllib timeouts to HTTP 504 Gateway Timeout and connection drops to HTTP 502 Bad Gateway. | M1 | Survey 2 (R2) |
| F7 | Circuit Breaker 502/504 Failover Trigger | Extend `is_exhaustion_error` / `is_failover_error` in `tool_choice_guard.py` to trigger on HTTP 502 and 504, transitioning circuit breaker to `STATE_FAILOVER`. | M2 | Survey 2 (R2) |
| F8 | Trailing Hook Normalization & Prompt Termination | Re-order trailing system hook messages (e.g. Caveman mode) to precede active user turn, ensuring flattened prompts always end with explicit `USER:` invocation and `ASSISTANT:` cue. | M1 | Survey 2 (R3) |
| F9 | Prompt Sanitization & Bloat Reduction | Deduplicate redundant historical `CAVEMAN MODE ACTIVE` hook reminders (retaining latest) and strip ephemeral `<!-- buddy: ... -->` statusline comments. | M1 | Survey 2 (R3) |
| F10 | Ephemeral Mock Test Infrastructure | Mock Language Server and Mock Remote Fallback server on dynamic ports (`127.0.0.1:0`), plus turn extractor for session `7f8bfd5b`. | E2E-Track | Survey 3 (R4) |
| F11 | Ephemeral Test Suite (Tiers 1-4) | Comprehensive test suite verifying tool calling emulation, fail-fast 502/504 errors, LiteLLM fallback routing, and hook normalization. | E2E-Track | Survey 3 (R4) |
| F12 | Historical Session 7f8bfd5b Replay Acceptance | Automated replay test executing extracted turns of session `7f8bfd5b` with 0 empty turns and 0 synthetic error recovery loops. | E2E-Track / M3 | Survey 3 (R4) |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Bridge Core Hardening | Implement F1, F2, F3, F4, F5, F6, F8, F9 in `service/bin/antigravity_bridge.py`. | none | DONE |
| M2 | Circuit Breaker Failover Hardening | Implement F7 in `tool_choice_guard.py`. | none | DONE |
| E2E | E2E Testing Track | Implement F10, F11, F12 in `tests/` and publish `TEST_READY.md`. | none (independent) | DONE |
| M3 | Final Acceptance & Adversarial Hardening | Phase 1: 100% pass on E2E replay suite (Tiers 1-4). Phase 2: Adversarial coverage hardening (Tier 5). | M1, M2, E2E | DONE |

## Interface Contracts

### Downstream Client ↔ `antigravity_bridge.py`
- **Request**: OpenAI compatible `POST /v1/chat/completions`
  - Body fields: `messages` (list of dicts), `model` (str), `stream` (bool, optional), `tools` (list of OpenAI tool dicts, optional), `tool_choice` (str/dict, optional).
- **Non-Streaming Response**:
  - Success with Text: HTTP 200 `{"id": "...", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}]}`
  - Success with Tool Calls: HTTP 200 `{"id": "...", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": null, "tool_calls": [{"id": "call_...", "type": "function", "function": {"name": "...", "arguments": "{...}"}}]}, "finish_reason": "tool_calls"}]}`
  - Upstream Empty Response / Failure: HTTP 502 Bad Gateway `{"error": {"message": "Upstream model returned empty completion", "type": "bad_gateway", "code": 502}}`
  - Upstream Timeout: HTTP 504 Gateway Timeout `{"error": {"message": "Upstream language server timed out", "type": "gateway_timeout", "code": 504}}`
- **Streaming Response (SSE)**:
  - Header: `Content-Type: text/event-stream; charset=utf-8`
  - Text Chunk: `data: {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "..."}, "finish_reason": null}]}\n\n`
  - Tool Call Chunk (segregated): `data: {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_...", "type": "function", "function": {"name": "...", "arguments": "{...}"}}]}, "finish_reason": null}]}\n\n`
  - Terminal Chunk: `data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" | "stop"}]}\n\n`
  - End Marker: `data: [DONE]\n\n`

### `antigravity_bridge.py` ↔ Antigravity Language Server
- **Request**: `rpc("GetModelResponse", {"prompt": prompt_text, "model": internal_model})`
  - Must timeout after 60s (not hang indefinitely).
  - Empty response `{}` or `{"response": ""}` is treated as an upstream error.
- **Injected Demarcation Format**:
  - `<tool_call>\n{"name": "<name>", "arguments": <arguments_object>}\n</tool_call>`

### LiteLLM Router / `tool_choice_guard.py` ↔ `antigravity_bridge.py`
- Status codes triggering failover: HTTP 429, HTTP 502, HTTP 504.
- `is_exhaustion_error(original_exception)` / `is_failover_error`: returns `True` for 502 and 504 status codes.
- `circuit_breaker.record_failure()` moves state from `STATE_PRIMARY` to `STATE_FAILOVER`.

## Code Layout
- `service/bin/antigravity_bridge.py`: Language Server bridge (Owned by Milestone 1 Worker)
- `tool_choice_guard.py`: Circuit breaker & custom guard (Owned by Milestone 2 Worker)
- `config/config.yaml`: LiteLLM proxy configuration
- `tests/fixtures/session_7f8bfd5b_turns.json`: Replay test fixture (Owned by E2E Testing Worker)
- `tests/replay_harness.py`: Ephemeral mock server and replay utilities (Owned by E2E Testing Worker)
- `tests/test_session_replay.py`: Pytest suite for session replay (Owned by E2E Testing Worker)
- `tests/test_tool_choice_guard.py`: Unit tests for guard & circuit breaker
- `tests/test_config_validation.py`: Config schema tests
