# Test Infrastructure: Automated Ephemeral Session Replay Harness

## 1. Overview & Architecture

The E2E Replay Testing Harness (`tests/replay_harness.py`) provides an isolated, deterministic, and ephemeral testing environment for verifying `litellmproxy-multiharness` and `service/bin/antigravity_bridge.py`. It guarantees that all test execution occurs without dependency on running local application daemons, external networks, or fixed port allocations.

```
+-------------------------------------------------------------------------------+
|                      pytest Replay Test Suite (Tiers 1-4)                     |
|                         tests/test_session_replay.py                          |
+---------------------------------------+---------------------------------------+
                                        |
                 +----------------------+----------------------+
                 |                                             |
                 v (HTTP POST /v1/chat/completions)            v (Failover fallback)
+---------------------------------------+     +---------------------------------+
|       EphemeralBridge (127.0.0.1:0)   |     | MockRemoteFallbackServer        |
|   service/bin/antigravity_bridge.py   |     |         (127.0.0.1:0)           |
+-------------------+-------------------+     +---------------------------------+
                    |                                          ^
                    | (RPC GetModelResponse)                   | (on 502/504 failover)
                    v                                          |
+---------------------------------------+     +----------------+----------------+
|    MockLanguageServer (127.0.0.1:0)   |     |    AntigravityCircuitBreaker    |
|   (/healthz, GetModelResponse, etc.)  |     |     & StripOrphanToolChoiceGuard|
+---------------------------------------+     +---------------------------------+
```

---

## 2. Ephemeral Mock Servers

### 2.1 `MockLanguageServer` (`tests/replay_harness.py`)
Runs an ephemeral `ThreadingHTTPServer` bound to `127.0.0.1:0` simulating the local Antigravity Language Server RPC interface:

- **Endpoints**:
  - `GET /healthz`, `/health`: Responds with HTTP 200 `{"status": "ok"}` used by `discover_ls()` port readiness checks.
  - `POST /exa.language_server_pb.LanguageServerService/GetCascadeModelConfigData`: Responds with active model configurations catalog (`antigravity-flash`, `antigravity-pro`, `antigravity-remote-flash-medium`).
  - `POST /exa.language_server_pb.LanguageServerService/GetModelResponse`: Configurable completion simulator.
- **Supported Operational Modes**:
  - `normal`: Returns successful plain text completion.
  - `empty`: Returns empty JSON `{}` (triggers fail-fast HTTP 502 in bridge).
  - `empty_string`: Returns `{"response": ""}` (triggers fail-fast HTTP 502).
  - `whitespace`: Returns whitespace string `{"response": "   \n\t  "}` (triggers fail-fast HTTP 502).
  - `timeout`: Simulates upstream delays exceeding client timeout thresholds.
  - `rpc_error`: Returns HTTP 500 error payload.
  - `connection_drop`: Drops TCP connection immediately.
  - `tool_call`: Emits `<tool_call>{"name": "create_pieces_memory", "arguments": {"text": "IAESTE-Hamburg e.V."}}</tool_call>`.
  - `mixed_tool_call`: Emits conversational reasoning followed by `<tool_call>` block and closing text.
  - `multi_tool_call`: Emits multiple consecutive `<tool_call>` blocks.
  - `markdown_json_tool_call`: Encloses tool call inside markdown code fences ` ```json ... ``` `.
  - `partial_demarcation`: Emits unclosed tag `<tool_call>{"name": "incomplete"`.
  - `custom`: Supports arbitrary static response dicts or dynamic request-inspecting callables.
- **Request Inspection**:
  - Maintains thread-safe `recorded_requests` list containing request path, headers, parsed body, prompt, model, and timestamp.
  - Exposes `last_prompt` property for immediate prompt injection validation.

### 2.2 `MockRemoteFallbackServer` (`tests/replay_harness.py`)
Simulates the remote OpenAI-compatible fallback endpoint (`antigravity-remote-flash-medium`) on dynamic port `127.0.0.1:0`:

- **Endpoints**:
  - `GET /health`, `/healthz`: Responds with HTTP 200.
  - `POST /v1/chat/completions`: Supports both non-streaming JSON responses and streaming SSE chunks (`chat.completion.chunk`).
- **Telemetry**:
  - Records request history to verify that failover requests from LiteLLM / `tool_choice_guard.py` reach the fallback server and produce non-empty completions.

### 2.3 `EphemeralBridge` (`tests/replay_harness.py`)
Instantiates `service.bin.antigravity_bridge.Handler` on an OS-allocated dynamic port:

- Overrides bridge discovery caches (`_cached_ls_info` and `_cached_ls_expiry`) to wire directly to the active `MockLanguageServer`.
- Exposes `base_url`, `completions_url`, and managed synchronous / asynchronous `httpx` clients (`get_client()`, `get_async_client()`).
- Provides clean thread shutdown and socket cleanup on context exit.

---

## 3. Session 7f8bfd5b Replay Fixture

### 3.1 Path & Extraction
- **Fixture Path**: `tests/fixtures/session_7f8bfd5b_turns.json`
- **Origin**: Historical Claude Code session `/Users/thedawgctor/.claude/projects/-Users-thedawgctor-Desktop-tempfuk-session-finder/7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl` (112 entries, 12 user turns).
- **Extracted Content**:
  - `session_id`: `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93`
  - `total_raw_entries`: 112
  - `total_user_turns`: 12
  - `total_historical_failures`: 8 (Turns 0, 1, 6, 7, 8, 9, 10, 11)
  - `tools`: 5 core OpenAI schemas (`Write`, `Edit`, `Bash`, `AskUserQuestion`, `create_pieces_memory`)
  - `all_tool_names`: Full list of all 175 tool definitions present in session attachments
  - `turns`: Array of turn objects containing `turn_index`, `user_input`, `system_hooks` (`CAVEMAN MODE ACTIVE`), `is_synthetic_recovery`, `historical_output_tokens`, `historical_failure`, and ready-to-replay `messages`.

---

## 4. Test Suite Matrix (`tests/test_session_replay.py`)

| Tier | Test ID | Target Feature | Description / Assertions |
|---|---|---|---|
| **Tier 1** | `test_tier1_tool_schema_prompt_injection` | F1 | Validates that passing `tools` injects schema instructions and `<tool_call>` demarcation rules into prompt forwarded to Language Server, ending with `ASSISTANT:`. |
| **Tier 1** | `test_tier1_non_streaming_tool_call_response_parsing` | F2, F3 | Validates parsing of `<tool_call>` blocks into standard OpenAI `choices[0].message.tool_calls` with generated `call_...` IDs and `finish_reason: "tool_calls"`. |
| **Tier 1** | `test_tier1_streaming_sse_tool_call_chunks` | F3 | Validates SSE streaming emits segregated delta content, tool_calls chunks, finish_reason chunk, and `data: [DONE]`. |
| **Tier 1** | `test_tier1_upstream_empty_fail_fast_http_502` | F5 | Validates that upstream returning `{}`, `{"response": ""}`, or whitespace immediately raises HTTP 502 Bad Gateway instead of HTTP 200 with empty string. |
| **Tier 1** | `test_tier1_upstream_timeout_http_504` | F6 | Validates mapping socket/urllib read timeouts to HTTP 504 Gateway Timeout and connection drops to HTTP 502 Bad Gateway. |
| **Tier 1** | `test_tier1_prompt_flattening_trailing_hook_normalization` | F8, F9 | Validates reordering of terminal system hooks (e.g. Caveman mode) before active user turn, ensuring prompt terminates with `USER:` and `ASSISTANT:`. |
| **Tier 2** | `test_tier2_empty_messages_handling` | Robustness | Validates handling of empty messages arrays or empty content strings without unhandled 500 crashes. |
| **Tier 2** | `test_tier2_tool_call_missing_arguments` | Robustness | Validates `<tool_call>` missing `arguments` field defaults safely to `"{}"`. |
| **Tier 2** | `test_tier2_partial_or_malformed_demarcations` | Robustness | Validates unclosed `<tool_call>` or malformed JSON falls back gracefully to raw content. |
| **Tier 2** | `test_tier2_markdown_json_fenced_tool_call` | Parsing | Validates stripping markdown code fences (```json ... ```) enclosing `<tool_call>` blocks. |
| **Tier 2** | `test_tier2_multi_turn_tool_history_preservation` | F4 | Validates preserving previous assistant `tool_calls` and tool execution outputs in conversational history across turns. |
| **Tier 3** | `test_tier3_tool_calling_under_caveman_mode` | F1, F2, F9 | Validates tool calling when `CAVEMAN MODE ACTIVE` hook is present, ensuring deduplication of repeated reminders and correct tool execution. |
| **Tier 3** | `test_tier3_empty_upstream_triggers_circuit_breaker_and_fallback` | F5, F7 | Validates HTTP 502 from bridge transitions circuit breaker to `STATE_FAILOVER`, routing requests to `MockRemoteFallbackServer`. |
| **Tier 4** | `test_tier4_session_7f8bfd5b_fixture_integrity` | F10 | Validates JSON fixture structure, turn count, historical failure tracking, and tool schema schemas. |
| **Tier 4** | `test_tier4_session_7f8bfd5b_human_turns_replay` | F12 | Replays all 8 human user turns through the hardened bridge; asserts 100% of turns produce non-empty output, 0 turns fail with 0 tokens, and synthetic recovery is never provoked. |

---

## 5. Execution Instructions

The entire harness runs via `uv`:

```bash
# Run the E2E Replay Test Suite
uv run python -m pytest tests/test_session_replay.py -v

# Run the full project test suite
uv run python -m pytest tests/test_session_replay.py tests/test_tool_choice_guard.py tests/test_config_validation.py -v
```
