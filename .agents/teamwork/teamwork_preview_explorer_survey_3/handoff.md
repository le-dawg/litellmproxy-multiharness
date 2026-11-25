# Handoff Report: Automated Ephemeral Session Replay & Verification Harness (R4)

**Agent**: Explorer 3 (Investigation & Synthesis)  
**Date**: 2026-10-26  
**Working Directory**: `/Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/teamwork_preview_explorer_survey_3`  
**Handoff Type**: Hard (Task Complete)

---

## 1. Observation

### 1.1 Codebase & Test Environment
- **Project Configuration**:
  File: `/Users/thedawgctor/Desktop/litellmproxy-multiharness/pyproject.toml`
  Lines 7-14 list dependencies: `litellm[proxy]>=1.89.0`, `fastapi<0.140.0`, `pyyaml>=6.0`, `httpx>=0.27.0`, `pytest>=8.0.0`, `pytest-asyncio>=0.23.0`.
- **Existing Test Suite**:
  - `tests/test_tool_choice_guard.py` (35 tests, all passing):
    Running `uv run python -m pytest tests/test_tool_choice_guard.py` returned:
    `35 passed in 1.95s`.
  - `tests/test_config_validation.py` (7 tests, all passing):
    Running `uv run python -m pytest tests/test_config_validation.py` returned:
    `7 passed in 0.07s`.
  - `tests/test_live_proxy.py`:
    Running `uv run python -m pytest tests/test_live_proxy.py` showed 3 tests passing and 1 timeout failure on `test_clean_error_message_without_router_leak` when sending empty input to live proxy port 5596.
- **Pytest Shebang Desync**:
  Running bare `uv run pytest` yielded verbatim:
  `error: Failed to spawn: pytest. Caused by: No such file or directory (os error 2)`
  Inspecting `/Users/thedawgctor/Desktop/litellmproxy-multiharness/.venv/bin/pytest`:
  Line 1 is `#!/Users/thedawgctor/Desktop/litellm-simplified/.venv/bin/python`.
  The previous directory was renamed to `litellmproxy-multiharness`. Running `uv run python -m pytest` bypasses the script shebang and runs cleanly. Running `uv sync` fixes the shebang.

### 1.2 Target Historical Session
- **File Path**:
  `/Users/thedawgctor/.claude/projects/-Users-thedawgctor-Desktop-tempfuk-session-finder/7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl`
- **Total Lines**: 112 lines.
- **Turn Analysis**:
  - Turn 1 (idx 10): User `jazaka_` -> Assistant (idx 27) `output_tokens: 0, text: ""` -> Claude Code synthetic recovery (idx 29) `[Your previous response had no visible output...]` -> Assistant (idx 34) `output_tokens: 0, text: ""`.
  - Turn 6 (idx 78): User `i did not see you call memoery cration tools right ther e...` -> Assistant (idx 80) `output_tokens: 0, text: ""` -> Claude Code synthetic recovery (idx 81) `[Your previous response had no visible output...]` -> Assistant (idx 82) `output_tokens: 0, text: ""`.
  - Turn 7 (idx 88): User `bro wtf get to fecking work bro` -> Assistant (idx 91) `output_tokens: 0` -> Synthetic recovery (idx 92) -> Assistant (idx 93) `output_tokens: 0`.
  - Turn 8 (idx 99): User `one mor efailure and I will effect permadeath on u` -> Assistant (idx 101) `output_tokens: 0` -> Synthetic recovery (idx 102) -> Assistant (idx 103) `output_tokens: 0`.
- **Self-Contained Prompt Snapshot**:
  Line 28 attachment contains full schema of **175 tools** (`Bash`, `Edit`, `Write`, `create_pieces_memory`, etc.) and system prompts.

### 1.3 Traffic Log & Bridge Behavior
- **Traffic Log**: `/Users/thedawgctor/.litellm/antigravity-traffic.jsonl`
  Request `ag-b717b0e9fb684d9690943120051fbb80`:
  Prompt ends verbatim:
  `SYSTEM: UserPromptSubmit hook additional context: CAVEMAN MODE ACTIVE (caveman). Respond terse like smart caveman. All technical substance stay. Only fluff die. Answer only what was asked: no unrequested background, lists, examples, walkthroughs, or follow-up offers; give code, steps, or warnings when the task needs them. Security warnings, irreversible actions, multi-step order: normal prose. Technical terms, code, commands, paths, and errors stay exact.`
  Upstream RPC response:
  `{"ts": 1792948666.1051898, "kind": "upstream_response", "request_id": "ag-b717b0e9fb684d9690943120051fbb80", "status": 200, "body": {}}`
- **Bridge Implementation**: `/Users/thedawgctor/Desktop/litellmproxy-multiharness/service/bin/antigravity_bridge.py`
  - Lines 61-75: `messages_to_prompt` simply formats `f"{role}: {content}"` without checking if the prompt terminates in a bare system hook or appending an assistant cue.
  - Lines 270-275: `payload.get("tools")` is completely dropped; never passed or injected.
  - Lines 293-368: When `upstream_response` is `{}` (empty dict), `content = upstream_response.get("response", "")` sets `content = ""`, and line 368 responds with `self.respond_json(200, response_body)`. Returning HTTP 200 prevents LiteLLM Router from falling back to `antigravity-remote-flash-medium`.

---

## 2. Logic Chain

1. **Terminal Hook Induces Upstream Empty Response**:
   When `messages_to_prompt` concatenates multi-turn history ending in a `UserPromptSubmit` hook (Observation 1.3), the completion model is handed instructions terminating in `SYSTEM: CAVEMAN MODE ACTIVE...` with no `USER:` or `ASSISTANT:` trigger. The language server interprets this as unprompted instructions and emits `{}` (Observation 1.3).
2. **Bridge 200 OK Defeats Circuit Breaker & Fallback Routing**:
   Because `antigravity_bridge.py` wraps `{}` into an HTTP 200 OK chat completion response with empty content (Observation 1.3), LiteLLM proxy considers the request successful and does not invoke `router_settings.fallbacks` (`antigravity-remote-flash-medium`).
3. **Empty Completion Triggers Claude Code Recovery Loop**:
   LiteLLM forwards the HTTP 200 empty response to Claude Code as `output_tokens: 0`, `text: ""` (Observation 1.2). Claude Code's CLI runtime detects missing output and generates synthetic prompt `[Your previous response had no visible output. Please continue and produce a user-visible response.]`. Because the prompt state remains unchanged, upstream generation fails identically, creating an infinite 0-token loop (Observation 1.2).
4. **Tool Definitions Dropped by Bridge**:
   Claude Code requests include tool definitions (Observation 1.2, line 28). `antigravity_bridge.py` does not inject tool definitions into the prompt or parse `<tool_call>` outputs, silently discarding tool calls (Observation 1.3).
5. **Requirements for Ephemeral Replay Harness (R4)**:
   To prevent regressions without relying on external servers or live Antigravity.app:
   - An ephemeral test harness (`tests/test_session_replay.py` and `tests/replay_harness.py`) must extract recorded turns from `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl`.
   - The test must spin up an ephemeral mock language server on dynamic port `127.0.0.1:0`.
   - The test must verify that:
     1. Trailing hooks are sanitized and normalized so prompt ends with explicit user/assistant continuation.
     2. Empty upstream generation immediately returns HTTP 502 Bad Gateway.
     3. LiteLLM Router intercepts 502 and falls back to `antigravity-remote-flash-medium`.
     4. Tool definitions in `payload["tools"]` are injected into prompt and parsed from `<tool_call>...</tool_call>` into standard OpenAI `tool_calls` structure (`choices[0].message.tool_calls` and streaming `delta.tool_calls`).
     5. Full replay of `7f8bfd5b` succeeds with 0 empty turns and 0 synthetic error recovery loops.

---

## 3. Caveats

1. **Source Code Write Permission**: As an Explorer agent with read-only constraints, no modifications were made to project source code files (`antigravity_bridge.py`, `tool_choice_guard.py`, `config/config.yaml`, or `tests/`). All proposals are documented in `analysis.md` and this handoff.
2. **Virtual Environment Shebang**: Until `uv sync` is executed in the repository, tests must be run using `uv run python -m pytest` rather than `uv run pytest`.
3. **External Network / Port 10.147.17.130**: The production remote fallback (`http://10.147.17.130:8788/v1`) is an internal remote IP. In the ephemeral test environment, the test harness must spin up a local mock server on `127.0.0.1:0` to act as the remote fallback destination rather than attempting to connect to external IPs.

---

## 4. Conclusion

The root cause of the 0-token loop and tool stripping is fully diagnosed across the file system, network logs, and source code.
To implement R4:
1. Create `tests/fixtures/session_7f8bfd5b_turns.json`: A self-contained extract of the turns and tools from `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl`.
2. Create `tests/replay_harness.py`: Utilities to spin up an ephemeral mock language server, extract turns, and manage dynamic port allocation.
3. Create `tests/test_session_replay.py`: The pytest replay suite verifying:
   - Tool call injection & streaming/non-streaming parsing.
   - HTTP 502 fail-fast on upstream failure & LiteLLM automatic fallback.
   - Prompt flattening sanitization of trailing hooks.
   - 0-token loop prevention during replay of historical session `7f8bfd5b`.

---

## 5. Verification Method

Once implemented by the development team, run:
1. Re-sync environment shebangs:
   ```bash
   cd /Users/thedawgctor/Desktop/litellmproxy-multiharness && uv sync
   ```
2. Execute the automated replay verification tests:
   ```bash
   cd /Users/thedawgctor/Desktop/litellmproxy-multiharness && uv run python -m pytest tests/test_session_replay.py -v
   ```
3. Run the complete test suite:
   ```bash
   cd /Users/thedawgctor/Desktop/litellmproxy-multiharness && uv run python -m pytest tests/ -v
   ```

**Pass Criteria**:
- All tests pass with exit code 0.
- All turns replayed from `7f8bfd5b` complete with non-zero tokens.
- No `[Your previous response had no visible output...]` synthetic recovery messages are triggered.
