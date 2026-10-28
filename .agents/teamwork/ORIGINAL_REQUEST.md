# Original User Request

## 2026-10-26T00:14:28Z

Harden `litellmproxy-multiharness` and `antigravity_bridge.py` by implementing structured JSON tool-call emulation and fail-fast fallback handling, completely eliminating 0-token empty output loops and tool stripping, verified by replaying historical Claude Code sessions in isolated ephemeral test environments.

Working directory: `/Users/thedawgctor/Desktop/litellmproxy-multiharness`
Integrity mode: demo

## Requirements

### R1. In-Bridge Tool Calling Emulation & Streaming Parser
Implement full tool calling emulation inside `antigravity_bridge.py`:
- Inject incoming OpenAI tool definitions and schemas (`payload.get("tools")`) into the prompt with explicit structured JSON instructions and demarcation markers (e.g., `<tool_call>{"name": "...", "arguments": {...}}</tool_call>`).
- Parse model text responses for tool call demarcations in both streaming and non-streaming modes.
- Translate detected tool markers into standard OpenAI `tool_calls` structure (`choices[0].delta.tool_calls` / `choices[0].message.tool_calls`) with generated `call_id`s, ensuring downstream clients like Claude Code receive valid agentic tool actions.

### R2. Fail-Fast Upstream Error Handling & Fallback Circuit Breaker
Eliminate silent 0-token completions when upstream generation fails:
- If `GetModelResponse` returns empty `{}` or encounters an RPC/timeout error, `antigravity_bridge.py` must return HTTP 502 (Bad Gateway) or HTTP 504 (Gateway Timeout) rather than HTTP 200 with an empty string (`""`).
- Ensure LiteLLM's `StripOrphanToolChoiceGuard` circuit breaker and LiteLLM Router catch the 502/504 and automatically trigger the configured fallback routes (`antigravity-flash -> antigravity-remote-flash-medium`) without user intervention.

### R3. Prompt Flattening Sanitization & Trailing Hook Normalization
Normalize multi-turn prompt flattening in `messages_to_prompt`:
- Ensure that concatenated prompts always end with an explicit user invocation, preventing terminal `SYSTEM:` hook blocks (e.g. Caveman mode) from confusing completion models into generating empty outputs.
- Filter and deduplicate redundant system hook reminders (`CAVEMAN MODE ACTIVE`, statusline buddy tags) across conversational history to prevent context bloat.

### R4. Automated Ephemeral Session Replay & Verification Harness
Construct an automated replay test suite powered by `uv`:
- Extract recorded turns from historical Claude Code sessions (including session `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93` in `~/.claude/projects/`).
- Spin up ephemeral test client runs against the proxy pipeline.
- Verify programmatically that tool calls are successfully emitted, parsed, and delivered, and that no `[Your previous response had no visible output...]` loops can ever be triggered.

## Acceptance Criteria

### Tool Calling Emulation
- [ ] Requests containing tool definitions (e.g. `Write`, `Edit`, `Bash`) prompt the model with explicit tool schemas and syntax instructions.
- [ ] Model outputs containing tool call JSON are parsed into standard OpenAI `tool_calls` response chunks in streaming mode and `choices[0].message.tool_calls` in non-streaming mode.
- [ ] Claude Code receives executable tool invocations rather than raw discarded strings.

### Error Handling & Fallback
- [ ] Any empty or aborted upstream generation from Antigravity Language Server immediately returns HTTP 502 Bad Gateway.
- [ ] LiteLLM Router intercepts HTTP 502 and routes to configured fallback deployments without returning empty 200 OK chunks to the client.
- [ ] Claude Code's synthetic recovery prompt loop (`[Your previous response had no visible output...]`) never triggers.

### Session Replay Validation
- [ ] Replay test suite runs via `uv run pytest` or `uv run python`.
- [ ] Replaying the exact turns from `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93` against the hardened proxy succeeds without empty turns or tool failures.
- [ ] All verification tests pass with 0 failures in isolated test environments.
