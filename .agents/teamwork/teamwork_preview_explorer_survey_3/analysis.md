# Survey Analysis Report: Automated Ephemeral Session Replay & Verification Harness (R4)

**Explorer**: Explorer 3 (Investigation & Synthesis)  
**Date**: 2026-10-26  
**Target Repository**: `/Users/thedawgctor/Desktop/litellmproxy-multiharness`  
**Working Directory**: `/Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/teamwork_preview_explorer_survey_3`  
**Reference Target Session**: `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93` in `~/.claude/projects/-Users-thedawgctor-Desktop-tempfuk-session-finder/`  

---

## Executive Summary

Historical Claude Code session `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93` suffered repeated catastrophic 0-token empty output loops and tool call dropping due to a three-part failure chain:
1. **Unterminated / Trailing Hook Concatenation**: Terminal system hook blocks (specifically `UserPromptSubmit` injecting `CAVEMAN MODE ACTIVE (caveman)`) were concatenated at the very end of multi-turn prompts without an explicit user prompt marker or assistant turn prompt. The upstream language server (`exa.language_server_pb.LanguageServerService/GetModelResponse`) received prompts ending in bare system directives and returned an empty response `{}`.
2. **Silent 200 OK Empty String Fallback**: In `service/bin/antigravity_bridge.py`, when `GetModelResponse` returned `{}` or `""`, the bridge packaged it into an HTTP 200 OK chat completion response with `choices[0].message.content = ""`. Because HTTP 200 was returned, LiteLLM Router and `StripOrphanToolChoiceGuard` never detected an error and did not trigger the configured fallback route (`antigravity-flash -> antigravity-remote-flash-medium`).
3. **Synthetic Recovery Loop Cascade & Tool Stripping**: Claude Code received an Anthropic `/v1/messages` response with `output_tokens: 0` and empty text. Detecting no visible output, Claude Code automatically issued its synthetic recovery prompt: `[Your previous response had no visible output. Please continue and produce a user-visible response.]`. When upstream failed again, Claude Code looped continuously. Furthermore, `antigravity_bridge.py` completely discarded incoming `payload.get("tools")`, preventing Claude Code from executing agentic tool actions (such as memory creation or file operations).

To solve and continuously prevent this regression, Requirement 4 (R4) requires an automated, ephemeral session replay and verification harness running via `uv run pytest`. This survey details the file formats, execution environment, test architecture, mock pipelines, and programmatic verification points required to implement R4.

---

## 1. Codebase & Test Setup Investigation

### 1.1 Dependency & Environment Inspection (`pyproject.toml`, `uv.lock`)
The repository is managed using `uv` with Python 3.12 (`cpython-3.12-macos-aarch64-none`).
In `/Users/thedawgctor/Desktop/litellmproxy-multiharness/pyproject.toml`:
```toml
[project]
name = "litellm-simplified"
version = "0.2.0"
description = "Hardened LiteLLM proxy for Azure OpenAI GPT-5.4 with launchd daemon and parameter sanitization"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "litellm[proxy]>=1.89.0",
    "fastapi<0.140.0",
    "pyyaml>=6.0",
    "httpx>=0.27.0",
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
]
```
All required dependencies for an ephemeral test suite (`pytest`, `pytest-asyncio`, `httpx`, `fastapi`, `litellm[proxy]`, `pyyaml`) are already present in the project.

### 1.2 Virtualenv & Pytest Execution Observation
- **Observation**: Running bare `uv run pytest` in the repository failed with:
  ```
  error: Failed to spawn: `pytest`
    Caused by: No such file or directory (os error 2)
  ```
- **Root Cause**: The shebang line in `.venv/bin/pytest` points to a previous folder location:
  `#!/Users/thedawgctor/Desktop/litellm-simplified/.venv/bin/python`
  Because macOS cannot find the non-existent interpreter path in the shebang, the kernel returns `ENOENT` (os error 2).
- **Verification & Resolution**:
  Running pytest via python module:
  `uv run python -m pytest tests/test_tool_choice_guard.py`
  passed all 35 tests in 1.95s.
  Running `uv sync` in the repository updates all script shebangs to the current workspace root (`/Users/thedawgctor/Desktop/litellmproxy-multiharness/.venv/bin/python`), after which both `uv run pytest` and `uv run python -m pytest` execute cleanly.

### 1.3 Existing Test Suite Coverage
1. `tests/test_tool_choice_guard.py` (713 lines, 35 tests, all passing):
   - Unit tests for `StripOrphanToolChoiceGuard` and `AntigravityCircuitBreaker`.
   - Tests parameter stripping (`call_type`, `route_type`, `stop_sequences`), reasoning effort resolution, exhaustion error detection (429 / quota phrases), and failover states (`STATE_PRIMARY`, `STATE_FAILOVER`, `STATE_PROBING`).
2. `tests/test_config_validation.py` (140 lines, 7 tests, all passing):
   - Validates `config/config.yaml` router settings, model aliases, fallbacks (`antigravity-flash -> antigravity-remote-flash-medium`), and local port 8788 mappings.
3. `tests/test_live_proxy.py` (69 lines, 4 tests):
   - Targets a live daemon running on `127.0.0.1:5596`.

---

## 2. Investigation of Historical Claude Code Sessions

### 2.1 File Locations
- **Target Session File**:
  `/Users/thedawgctor/.claude/projects/-Users-thedawgctor-Desktop-tempfuk-session-finder/7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl`
- **Associated State & Configuration Files**:
  - Environment snapshot: `/Users/thedawgctor/.claude/session-env/7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93`
  - Caveman mode flag: `/Users/thedawgctor/.claude/.caveman-sessions/7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.mode`
  - Claude Code Settings: `/Users/thedawgctor/.claude/settings.json`
    Configures `ANTHROPIC_BASE_URL: http://127.0.0.1:5596`, `ANTHROPIC_MODEL: claude-gpt-5[1m]`, routing requests through LiteLLM proxy.
  - Proxy Traffic Log:
    `/Users/thedawgctor/.litellm/antigravity-traffic.jsonl` (13 MB log capturing actual upstream and incoming HTTP payloads).

### 2.2 Session JSONL Schema & Entry Types
The target session file contains **112 JSONL entries**:
| Entry Type | Count | Description / Role |
|---|---|---|
| `last-prompt` | 5 | Tracks latest prompt UUID and leaf pointer |
| `mode` | 4 | Session mode (e.g. `code`, `plan`) |
| `permission-mode` | 4 | Permissions mode (e.g. `plan`) |
| `atis-latch` | 4 | Latch state tracking |
| `attachment` | 42 | System prompts, prompt snapshots (tools schemas), hook outputs, environment snapshots |
| `file-history-snapshot`| 9 | Snapshots of workspace files modified during session |
| `user` | 12 | User turns: 6 human typed prompts + 6 synthetic recovery prompts |
| `assistant` | 12 | Assistant responses from `antigravity-flash` |
| `system` | 16 | Stop hook summaries and turn duration metrics |
| `ai-title` | 2 | Generated title metadata (`jazaka-polished-waterfall`) |
| `cost-state` | 2 | Token usage and cost tracking |

### 2.3 Detailed Structure of Key Turn Records

#### A. Prompt Snapshot (`type: "attachment"`, Line 28)
Line 28 contains a complete, self-contained record of the prompt environment:
- `systemPrompt`: List of system prompt strings establishing agent behavior.
- `tools`: An array of **175 tool definitions** with full JSON schemas (including `Bash`, `Edit`, `Write`, `AskUserQuestion`, `create_pieces_memory`, etc.).
Each tool definition contains:
```json
{
  "name": "Write",
  "description": "Writes a file to the local filesystem...",
  "schema": {
    "type": "object",
    "properties": {
      "file_path": {"type": "string", "description": "The absolute path to the file to write"},
      "content": {"type": "string", "description": "The content to write into the file"}
    },
    "required": ["file_path", "content"]
  }
}
```

#### B. Human User Turn vs Synthetic Recovery Turn
- **Human Turn** (e.g. Line 78):
  ```json
  {
    "type": "user",
    "message": {
      "role": "user",
      "content": "i did not see you call memoery cration tools right ther e..."
    },
    "isMeta": false,
    "turnPosition": {"promptIndex": 6, "turnIndex": 6},
    "uuid": "68aaa489-0311-42bb-921d-6a064677ce34"
  }
  ```
- **Assistant 0-Token Empty Turn** (e.g. Line 80):
  ```json
  {
    "type": "assistant",
    "message": {
      "id": "msg_929659bf-3503-44b5-80d2-f6ee422d3592",
      "type": "message",
      "role": "assistant",
      "content": [{"type": "text", "text": ""}],
      "model": "antigravity-flash",
      "stop_reason": "end_turn",
      "usage": {
        "input_tokens": 14300,
        "output_tokens": 0
      }
    },
    "uuid": "188ce431-4e51-458b-9c32-247ddf9c00d5"
  }
  ```
- **Claude Code Synthetic Recovery Turn** (e.g. Line 81):
  ```json
  {
    "type": "user",
    "message": {
      "role": "user",
      "content": "[Your previous response had no visible output. Please continue and produce a user-visible response.]"
    },
    "isMeta": true,
    "turnCompanion": true,
    "uuid": "7de4117d-2c1d-4357-979b-c8bb9e8384bd"
  }
  ```

---

## 3. Root Cause Analysis of the Regression

Correlating `/Users/thedawgctor/.litellm/antigravity-traffic.jsonl` with `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl`:

1. **Traffic Log Entry `ag-b717b0e9fb684d9690943120051fbb80`**:
   - `kind: "incoming_request"`: Received prompt of length 69,053 characters.
   - The end of the prompt was:
     ```
     l memoery cration tools right ther e...

     SYSTEM: UserPromptSubmit hook additional context: CAVEMAN MODE ACTIVE (caveman). Respond terse like smart caveman. All technical substance stay. Only fluff die. Answer only what was asked: no unrequested background, lists, examples, walkthroughs, or follow-up offers; give code, steps, or warnings when the task needs them. Security warnings, irreversible actions, multi-step order: normal prose. Technical terms, code, commands, paths, and errors stay exact.
     ```
   - Notice: Prompt ended with `SYSTEM: ...` without an assistant continuation token (`ASSISTANT:`).
   - `kind: "upstream_response"`: Upstream RPC `GetModelResponse` returned `{}` (empty JSON object).
2. **Bridge Behavior (`service/bin/antigravity_bridge.py`)**:
   ```python
   upstream_response = rpc("GetModelResponse", upstream_payload)
   content = upstream_response.get("response", "")  # Evaluates to ""
   ...
   response_body = {
       "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]
   }
   self.respond_json(200, response_body)  # Returns HTTP 200 OK!
   ```
   The bridge returned HTTP 200 with `content: ""` instead of failing fast with HTTP 502/504.
3. **LiteLLM Forwarding**:
   LiteLLM translated the HTTP 200 completion into Anthropic message `output_tokens: 0`, `text: ""`.
4. **Tool Stripping**:
   `antigravity_bridge.py` never inspected `payload.get("tools")` or `payload.get("tool_choice")`. Tool definitions sent by Claude Code were ignored and stripped.

---

## 4. Architecture of the Automated Ephemeral Session Replay Harness

To verify R1, R2, R3, and R4 reliably in isolated test environments without external dependencies or live Antigravity.app processes, the harness is structured into three decoupled layers:

```
+-----------------------------------------------------------------------------------------+
|                               pytest Replay Test Suite                                   |
|                             (tests/test_session_replay.py)                               |
+--------------------------------------------+--------------------------------------------+
                                             |
                                             v
                      +---------------------------------------------+
                      |         Turn Extractor & Replay Harness     |
                      |            (tests/replay_harness.py)        |
                      +----------------------+----------------------+
                                             |
            +--------------------------------+--------------------------------+
            |                                                                 |
            v                                                                 v
+-----------------------+                                         +-----------------------+
|  LiteLLM Test Client  |                                         |  Direct Bridge Client |
| (Proxy / Router Async)|                                         |  (HTTP/JSON TestClient)
+-----------+-----------+                                         +-----------+-----------+
            |                                                                 |
            | (routes to primary or fallback)                                 |
            v                                                                 v
+---------------------------------------+                         +-----------------------+
|     Ephemeral Bridge Server           |                         | Ephemeral Bridge      |
|    (service/bin/antigravity_bridge.py)|                         | (Port 127.0.0.1:bridge)
+-------------------+-------------------+                         +-----------+-----------+
                    |                                                         |
                    v                                                         |
+---------------------------------------+                                     |
|     Ephemeral Mock Language Server    |<------------------------------------+
| (Emulates GetModelResponse: empty,    |
|  tool_calls, streaming chunks, text)  |
+---------------------------------------+
                    ^
                    | (on 502 Bad Gateway fallback)
+-------------------+-------------------+
| Ephemeral Mock Remote Fallback Server |
|  (antigravity-remote-flash-medium)    |
+---------------------------------------+
```

### 4.1 Component Specifications

#### A. Session Turn Extractor (`tests/replay_harness.py`)
- **Input**: A Claude Code `.jsonl` session file path or fixture.
- **Functions**:
  - `load_session_turns(jsonl_path: Path) -> List[TurnRecord]`
  - `extract_prompt_snapshot_tools(jsonl_path: Path) -> List[dict]`
  - `extract_reproducible_dialogue(jsonl_path: Path) -> List[TurnPair]`
- Filters out synthetic `[Your previous response had no visible output...]` messages from test inputs so the test replays clean user requests and asserts that synthetic recovery is never provoked.
- Packages turns into standard OpenAI `/v1/chat/completions` and Anthropic `/v1/messages` request structures.

#### B. Ephemeral Mock Antigravity Language Server
- Runs an HTTP server on `127.0.0.1:0` (ephemeral port assigned by OS).
- Handles RPC path `/exa.language_server_pb.LanguageServerService/GetModelResponse` and `/exa.language_server_pb.LanguageServerService/GetCascadeModelConfigData`.
- Configurable response modes:
  1. `EMPTY`: returns `{}` (triggers fail-fast 502 Bad Gateway in bridge).
  2. `RPC_ERROR`: returns HTTP 500 / drops connection / times out.
  3. `TOOL_CALL`: returns `<tool_call>{"name": "create_pieces_memory", "arguments": {"text": "IAESTE-Hamburg e.V."}}</tool_call>`.
  4. `TEXT_WITH_TOOL`: returns `Thinking completed.\n<tool_call>{"name": "Write", "arguments": {"file_path": "/tmp/test.txt", "content": "hello"}}</tool_call>\nFile created.`.
  5. `NORMAL_TEXT`: returns normal text string.

#### C. Ephemeral Antigravity Bridge
- Spun up in a background thread or process with:
  `ANTIGRAVITY_BRIDGE_HOST="127.0.0.1"`, `ANTIGRAVITY_BRIDGE_PORT="0"` (or dynamically assigned free port).
- Replaces static `discover_ls()` with dynamic mock language server address passed via environment variable or constructor.
- Implements:
  - Tool injection into prompt from `payload["tools"]`.
  - Non-streaming tool call parsing (`choices[0].message.tool_calls`, `finish_reason="tool_calls"`).
  - Streaming tool call SSE chunk parsing (`choices[0].delta.tool_calls`).
  - Fail-fast HTTP 502 Bad Gateway when upstream returns `{}`.
  - Multi-turn prompt sanitization ending with `USER: ...\n\nASSISTANT:`.

#### D. Ephemeral Remote Fallback Server
- Runs on `127.0.0.1:0`.
- Represents `antigravity-remote-flash-medium` (`http://10.147.17.130:8788/v1` in production).
- When local bridge returns HTTP 502, LiteLLM router forwards request to this server, returning a valid 200 completion.

---

## 5. Verification Points & Test Case Matrix

The verification suite in `tests/test_session_replay.py` must programmatically enforce the following test cases:

| Test ID | Test Function | Purpose / Assertion | Expected Result |
|---|---|---|---|
| **V1** | `test_session_jsonl_extraction` | Load `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl`. Verify turn parsing, tool definitions extraction (175 tools), and detection of historical empty assistant responses. | Correctly parses all 112 lines, extracts tools, and flags turns 1, 6, 7, 8 as historical 0-token failures. |
| **V2** | `test_prompt_flattening_trailing_hook_normalization` | Pass Turn 6 messages ending with `CAVEMAN MODE ACTIVE` hook to `messages_to_prompt`. | Prompt never ends with `SYSTEM:`. Terminal hook is folded into system instructions or preceded by user prompt; terminates with `ASSISTANT:`. Deduplicates repeated hook lines across turns. |
| **V3** | `test_bridge_non_streaming_tool_call_emulation` | POST request with `tools` schema (e.g. `create_pieces_memory`, `Write`) and prompt to bridge. Upstream mock emits `<tool_call>{"name": "create_pieces_memory", "arguments": {"text": "IAESTE-Hamburg e.V."}}</tool_call>`. | Bridge returns HTTP 200 with `finish_reason: "tool_calls"`, `tool_calls[0].id` starting with `call_`, function name `create_pieces_memory`, arguments parsed as valid JSON string. No empty text. |
| **V4** | `test_bridge_streaming_tool_call_emulation` | POST request with `stream: true` and `tools` schema. Upstream mock emits chunks containing `<tool_call>...</tool_call>`. | Bridge yields SSE `chat.completion.chunk` events containing `delta.tool_calls` with incremental index and arguments, terminating with `finish_reason: "tool_calls"` followed by `data: [DONE]`. |
| **V5** | `test_bridge_fail_fast_on_empty_upstream` | Upstream mock returns `{}` or raises RPC error. POST to `/v1/chat/completions`. | Bridge returns HTTP 502 Bad Gateway (NOT HTTP 200 with `""`). Error payload specifies upstream failure. |
| **V6** | `test_litellm_router_automatic_fallback_on_502` | Configure LiteLLM Router with primary bridge and fallback server. Trigger upstream empty response on primary. | LiteLLM catches 502 from primary, executes fallback route to `antigravity-remote-flash-medium`, and returns HTTP 200 non-empty completion to client. |
| **V7** | `test_replay_historical_session_7f8bfd5b_full` | Replay all human user turns extracted from `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93` through the hardened proxy pipeline. | 100% of turns succeed with non-zero tokens. Zero empty turns. Claude Code synthetic prompt `[Your previous response had no visible output...]` is never emitted. |

---

## 6. Implementation Proposals for the Implementer

### 6.1 Session Turn Replay Fixture Extract
Store a sanitized JSON fixture extract of session `7f8bfd5b` under `tests/fixtures/session_7f8bfd5b_turns.json` containing:
1. `session_id`: `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93`
2. `tools`: subset of key tools (`Write`, `Edit`, `Bash`, `create_pieces_memory`)
3. `turns`: array of turn objects with `prompt_index`, `user_input`, `system_hooks`, and historical failure flag.
This ensures the test suite can execute anywhere (CI, new clones) even without access to `~/.claude/projects/`.

### 6.2 Code Blueprint: `tests/replay_harness.py`
```python
"""Ephemeral Replay Harness for historical Claude Code sessions."""
import json
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

def get_free_port() -> int:
    """Find a dynamic free port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

class MockLanguageServerHandler(BaseHTTPRequestHandler):
    mode = "normal"  # "empty", "tool_call", "rpc_error", "normal"
    last_prompt = ""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        MockLanguageServerHandler.last_prompt = body.get("prompt", "")

        if self.path.endswith("GetModelResponse"):
            if MockLanguageServerHandler.mode == "empty":
                self.respond_json(200, {})
                return
            if MockLanguageServerHandler.mode == "rpc_error":
                self.respond_json(500, {"error": "Simulated upstream RPC failure"})
                return
            if MockLanguageServerHandler.mode == "tool_call":
                resp_text = (
                    '<tool_call>{"name": "create_pieces_memory", '
                    '"arguments": {"text": "IAESTE-Hamburg e.V."}}</tool_call>'
                )
                self.respond_json(200, {"response": resp_text})
                return
            self.respond_json(200, {"response": "Turn processed successfully."})
            return

        self.respond_json(404, {"error": "Not Found"})

    def respond_json(self, status: int, data: dict):
        buf = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(buf)))
        self.end_headers()
        self.wfile.write(buf)

    def log_message(self, *args):
        pass
```

### 6.3 Code Blueprint: `tests/test_session_replay.py`
```python
"""Automated session replay verification tests."""
import pytest
import httpx
from tests.replay_harness import get_free_port, MockLanguageServerHandler

@pytest.mark.asyncio
async def test_bridge_returns_502_on_empty_upstream(ephemeral_bridge):
    """Verify bridge returns 502 Bad Gateway when upstream generation is empty."""
    MockLanguageServerHandler.mode = "empty"
    async with httpx.AsyncClient(base_url=ephemeral_bridge.base_url) as client:
        resp = await client.post("/v1/chat/completions", json={
            "model": "antigravity-flash",
            "messages": [{"role": "user", "content": "i did not see you call memoery cration tools"}]
        })
    assert resp.status_code == 502, f"Expected HTTP 502, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "error" in data
```

---

## 7. Verification Method

To independently execute and verify the test harness after implementation:
1. Ensure virtual environment shebangs are active:
   `cd /Users/thedawgctor/Desktop/litellmproxy-multiharness && uv sync`
2. Run the full test suite including the replay harness:
   `cd /Users/thedawgctor/Desktop/litellmproxy-multiharness && uv run python -m pytest tests/ -v`
3. Run the specific replay verification tests:
   `cd /Users/thedawgctor/Desktop/litellmproxy-multiharness && uv run python -m pytest tests/test_session_replay.py -v`

Expected outcome:
- 0 failures across all tests.
- Replaying `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93` turns succeeds without any 0-token empty output or unhandled tool calls.
