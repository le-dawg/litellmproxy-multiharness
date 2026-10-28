# Handoff Report: Survey Phase — R1 In-Bridge Tool Calling Emulation & Streaming Parser

**Agent:** Explorer Survey 1  
**Working Directory:** `/Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/teamwork_preview_explorer_survey_1`  
**Timestamp:** 2026-10-26T00:26:30Z  
**Type:** Hard Handoff (Survey Complete)  

---

## 1. Observation

### 1.1 Antigravity Language Server Protocol Constraints
- **File:** `/Applications/Antigravity.app/Contents/Resources/bin/language_server`
- **Method:** `strings` inspection of Protocol Buffer descriptors:
  ```text
  GetModelResponseRequest
  prompt
  model
  GetModelResponseResponse
  response
  ```
- **Verbatim descriptor strings:**
  `/exa.language_server_pb.LanguageServerService/GetModelResponse`
  `/.exa.language_server_pb.GetModelResponseRequest`
  `0.exa.language_server_pb.GetModelResponseResponse`
- **Result:** The protobuf definition for `GetModelResponseRequest` accepts ONLY `prompt` (string) and `model` (enum). It contains no message fields for `tools`, `functions`, or streaming flags.

### 1.2 Tool Stripping in `service/bin/antigravity_bridge.py`
- **File:** `/Users/thedawgctor/Desktop/litellmproxy-multiharness/service/bin/antigravity_bridge.py`
- **Lines 269–285:**
  ```python
  payload = json.loads(raw.decode("utf-8")) if raw else {}
  request_id = f"ag-{uuid.uuid4().hex}"
  prompt = messages_to_prompt(payload.get("messages") or [])
  requested_model, internal_model = resolve_model_name(payload.get("model"))
  ...
  upstream_payload = {"prompt": prompt, "model": internal_model}
  ```
  `payload.get("tools")` and `payload.get("tool_choice")` are discarded without inspection.
- **Lines 61–74:**
  ```python
  def messages_to_prompt(messages):
      ...
      parts = []
      for message in messages:
          if not isinstance(message, dict):
              continue
          role = (message.get("role") or "user").upper()
          content = flatten_content(message.get("content"))
          if content:
              parts.append(f"{role}: {content}")
      return "\n\n".join(parts)
  ```
  When downstream clients return an assistant turn with `content: null` and `tool_calls: [...]`, `flatten_content` evaluates to `""`. The entire message is dropped (`if content:` evaluates to `False`).

### 1.3 Streaming Response Limitations in `service/bin/antigravity_bridge.py`
- **Lines 307–344:**
  ```python
  chunk1 = {
      "id": cmpl_id,
      "object": "chat.completion.chunk",
      "created": created_ts,
      "model": requested_model,
      "choices": [
          {
              "index": 0,
              "delta": {"role": "assistant", "content": content},
              "finish_reason": None,
          }
      ],
  }
  chunk2 = {
      "id": cmpl_id,
      "object": "chat.completion.chunk",
      "created": created_ts,
      "model": requested_model,
      "choices": [
          {
              "index": 0,
              "delta": {},
              "finish_reason": "stop",
          }
      ],
  }
  ```
  The bridge emits only two chunks with hardcoded `content: content` and `finish_reason: "stop"`. It has no support for emitting `delta.tool_calls` or `finish_reason: "tool_calls"`.

### 1.4 Live Empirical Model Test on Antigravity Language Server
- **Command:** Executed prompt injection test via `antigravity_bridge.rpc("GetModelResponse", ...)` against live Language Server (PID 84261, port 49407).
- **Prompt:**
  ```text
  SYSTEM: You have access to the following tools:
  <tool>
  name: Write
  description: Writes a file to disk
  parameters: {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}
  </tool>

  To invoke a tool, output:
  <tool_call>
  {"name": "Write", "arguments": {"path": "...", "content": "..."}}
  </tool_call>

  USER: Please create a file called /tmp/hello.txt with the content 'Hello World'.

  ASSISTANT:
  ```
- **Verbatim Model Output:**
  ```xml
  <tool_call>
  {"name": "Write", "arguments": {"path": "/tmp/hello.txt", "content": "Hello World"}}
  </tool_call>
  ```
- In a second test with mixed conversational reasoning, the model emitted introductory text followed by `<tool_call>{"name": "Bash", "arguments": {"command": "ls -la /tmp"}}</tool_call>`.

### 1.5 Historical Empty Response Loop in Claude Code Sessions
- **File:** `/Users/thedawgctor/.claude/projects/-Users-thedawgctor-Desktop-tempfuk-session-finder/7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl`
- **Lines 27, 29, 80, 81, 91, 92:**
  Assistant emitted:
  ```json
  [{"type": "text", "text": ""}]
  ```
  Triggering user synthetic recovery prompt:
  ```text
  [Your previous response had no visible output. Please continue and produce a user-visible response.]
  ```
- **Correlated Traffic Log (`~/.litellm/antigravity-traffic.jsonl`):**
  Matches requests ending with `SYSTEM: UserPromptSubmit hook additional context: CAVEMAN MODE ACTIVE...` where `GetModelResponse` returned empty `{}`. The bridge wrapped this into HTTP 200 with `content: ""` instead of failing fast.

---

## 2. Logic Chain

1. **Premise 1:** The Antigravity Language Server RPC accepts only raw text `prompt` and `model` (Observation 1.1).  
   *Inference:* Native tool calling via protobuf request attributes is impossible. Tool calling must be achieved by in-prompt schema injection and textual demarcation.
2. **Premise 2:** The running Language Server strictly follows in-prompt `<tool_call>...</tool_call>` instructions, outputting valid JSON arguments both alone and with preceding conversational text (Observation 1.4).  
   *Inference:* In-bridge prompt injection and response parsing is technically sound, empirically proven, and viable.
3. **Premise 3:** `antigravity_bridge.py` currently drops `tools` from the payload, strips previous assistant `tool_calls` during prompt flattening, and only streams hardcoded text chunks with `finish_reason: "stop"` (Observations 1.2, 1.3).  
   *Inference:* Clients like Claude Code never receive agentic tool actions and lose conversational context after the first tool execution turn.
4. **Premise 4:** LiteLLM's Anthropic adapter (`_CombinedChunkSplitter` in `adapters/streaming_iterator.py`) converts OpenAI `choices[0].delta.tool_calls` into Anthropic `tool_use` blocks and `finish_reason: "tool_calls"` into `stop_reason: "tool_use"`, provided `content` and `tool_calls` deltas are delivered in separate chunks.  
   *Inference:* Emitting standard OpenAI chunks (`delta.content` first if text is present, then `delta.tool_calls` with generated `call_id`, then `finish_reason: "tool_calls"`) will seamlessly deliver executable tool calls to Claude Code.
5. **Premise 5:** Terminal `SYSTEM:` hooks cause completion models to return empty `{}` (Observation 1.5). Returning HTTP 200 with `""` traps Claude Code in a `[Your previous response had no visible output...]` loop.  
   *Inference:* Prompt flattening must guarantee an `ASSISTANT:` prompt termination (R3), and empty upstream responses must fail fast with HTTP 502 (R2) so LiteLLM router triggers fallback deployments.

---

## 3. Caveats

1. **No Source Modification in Survey Phase:** No code in `service/bin/antigravity_bridge.py` was altered during this phase (compliance with read-only explorer constraint).
2. **Unary Upstream RPC:** The upstream Language Server `GetModelResponse` call is unary, not streaming. Tokens are not received from the Language Server incrementally; the bridge receives the full text and synthesizes SSE chunks for downstream clients.
3. **JSON Argument Escaping:** Models occasionally emit JSON arguments containing unescaped newlines or quotes; the parser must sanitize markdown code fences (```json) and handle JSON decoding gracefully.

---

## 4. Conclusion

The Survey Phase for **R1: In-Bridge Tool Calling Emulation & Streaming Parser** is complete.
- **Root Cause Confirmed:** Language Server lacks native tool RPC fields; `antigravity_bridge.py` discards tools, drops assistant tool turns, and mocks streaming with 2 static chunks.
- **Viability Verified:** Live Language Server generates structured tool calls accurately under `<tool_call>` demarcation.
- **Implementation Strategy Defined:**
  1. `format_tool_instructions()` to serialize incoming OpenAI tool definitions into prompt instructions.
  2. `messages_to_prompt()` refactoring to retain prior `tool_calls` and tool execution outputs, ending with `ASSISTANT:`.
  3. `parse_model_response()` state machine to extract `<tool_call>` blocks and separate conversational text.
  4. Dual-mode response synthesizer emitting `choices[0].message.tool_calls` (non-streaming) and segregated SSE chunks for `content`, `tool_calls`, and `finish_reason: "tool_calls"` (streaming).

Comprehensive architectural documentation and code signatures have been recorded in:
`/Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/teamwork_preview_explorer_survey_1/analysis.md`

---

## 5. Verification Method

To independently verify the observations and conclusions:

1. **Verify Language Server Protobuf Schema:**
   ```bash
   strings /Applications/Antigravity.app/Contents/Resources/bin/language_server | grep -A 5 "GetModelResponseRequest"
   ```
   *Expected:* Shows `prompt` and `model` as the only request fields.

2. **Verify Live Tool Demarcation on Active Language Server:**
   ```bash
   cd /Users/thedawgctor/Desktop/litellmproxy-multiharness
   uv run python -c "
   import sys; sys.path.insert(0, 'service/bin'); import antigravity_bridge
   model = antigravity_bridge.load_model_map()['antigravity-flash']
   prompt = '''SYSTEM: You have access to tool Write: {\"path\": \"string\"}. Output <tool_call>{\"name\": \"Write\", \"arguments\": {\"path\": \"/tmp/a\"}}</tool_call>
   USER: Write file
   ASSISTANT:'''
   print(antigravity_bridge.rpc('GetModelResponse', {'prompt': prompt, 'model': model})['response'])
   "
   ```
   *Expected:* Output contains `<tool_call>{"name": "Write", "arguments": {"path": "/tmp/a"}}</tool_call>`.

3. **Verify Existing Unit Test Baseline:**
   ```bash
   cd /Users/thedawgctor/Desktop/litellmproxy-multiharness
   uv run --project . python -m pytest tests/test_config_validation.py tests/test_tool_choice_guard.py
   ```
   *Expected:* All 42 tests pass.
