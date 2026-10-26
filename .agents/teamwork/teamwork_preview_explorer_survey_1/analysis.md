# Architectural Survey: In-Bridge Tool Calling Emulation & Streaming Parser (R1)

**Target System:** `service/bin/antigravity_bridge.py` & `litellmproxy-multiharness`  
**Author:** Explorer Survey 1  
**Timestamp:** 2026-10-26T00:26:00Z  
**Status:** Comprehensive Analysis Complete  

---

## 1. Executive Summary

This survey analyzes the architecture of `service/bin/antigravity_bridge.py` and establishes the definitive technical blueprint for **R1: In-Bridge Tool Calling Emulation & Streaming Parser**, addressing the root causes of silent tool stripping, agent stalling, and empty 0-token response loops in Claude Code and LiteLLM workflows.

### Core Discoveries
1. **The Antigravity Language Server Has No Native Tool Calling Protocol:**  
   Inspection of the active binary (`/Applications/Antigravity.app/Contents/Resources/bin/language_server`) and its Protocol Buffer definition reveals that `exa.language_server_pb.GetModelResponseRequest` contains *only two fields*: `prompt` (string) and `model` (enum). Its response message `GetModelResponseResponse` contains *only* `response` (string). Upstream models (Gemini Flash/Pro, Claude Sonnet/Opus via Antigravity) cannot receive native OpenAI or Anthropic tool schemas directly through this RPC. Tool calling **must** be emulated within `antigravity_bridge.py`.
2. **Current Bridge Completely Strips Incoming Tools:**  
   In `antigravity_bridge.py` (lines 269–285), incoming requests discard `payload.get("tools")` and `payload.get("tool_choice")`. Furthermore, in multi-turn history (`messages_to_prompt`, lines 61–75), any assistant message with `content: null` and `tool_calls: [...]` is discarded completely (`flatten_content` returns `""`, which evaluates to falsy), breaking tool call context.
3. **Current Streaming Implementation is a 2-Chunk Mock:**  
   In `antigravity_bridge.py` (lines 307–344), streaming requests emit exactly two SSE chunks: one carrying the raw text string from the Language Server, and a second empty chunk with `finish_reason: "stop"`. It has zero support for `choices[0].delta.tool_calls` or `finish_reason: "tool_calls"`.
4. **Empirical Verification of In-Prompt Demarcation:**  
   Live testing against the running Language Server (PID 84261 on `127.0.0.1:49407`) demonstrated that injecting structured JSON tool schemas and explicit demarcation markers (`<tool_call>{"name": "...", "arguments": {...}}</tool_call>`) into the prompt results in reliable, high-fidelity tool call generation by the model, including support for mixed conversational text and structured tool calls.

---

## 2. Current Implementation Analysis: `antigravity_bridge.py`

### 2.1 Process & Network Architecture
`antigravity_bridge.py` runs an HTTP/1.1 daemon listening on `127.0.0.1:8788`:
```python
# Lines 15-16, 391-394
HOST = os.environ.get("ANTIGRAVITY_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ANTIGRAVITY_BRIDGE_PORT", "8788"))

def main():
    log_service(f"starting antigravity bridge on {HOST}:{PORT}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()
```
- Multi-threading: Each request runs in an isolated worker thread spawned by `ThreadingHTTPServer`.
- Upstream Target: Local Antigravity Language Server discovered dynamically via `discover_ls()` scanning process tables (`ps -ax`) for `--csrf_token` and listening ports via `lsof`.

### 2.2 Endpoint Dispatch (`Handler`)
- `GET /health`, `/healthz`, `/v1/health`: Returns HTTP 200 with Language Server address.
- `GET /v1/models`: Catalogs dynamic model IDs from `GetCascadeModelConfigData`.
- `POST /v1/chat/completions`: Main inference endpoint consumed by LiteLLM (lines 264–375).

### 2.3 Detailed Execution Trace in `do_POST`
```python
# Lines 269-294
length = int(self.headers.get("Content-Length", "0"))
raw = self.rfile.read(length)
payload = json.loads(raw.decode("utf-8")) if raw else {}
request_id = f"ag-{uuid.uuid4().hex}"
prompt = messages_to_prompt(payload.get("messages") or [])
requested_model, internal_model = resolve_model_name(payload.get("model"))

upstream_payload = {"prompt": prompt, "model": internal_model}
upstream_response = rpc("GetModelResponse", upstream_payload)
content = upstream_response.get("response", "")
```
**Vulnerabilities & Limitations Identified:**
1. **Tool Parameter Loss:** `payload.get("tools")` is ignored; models are never informed of available capabilities.
2. **Assistant Turn Stripping:**
   ```python
   # Lines 66-74
   parts = []
   for message in messages:
       role = (message.get("role") or "user").upper()
       content = flatten_content(message.get("content"))
       if content:
           parts.append(f"{role}: {content}")
   return "\n\n".join(parts)
   ```
   If an assistant message generated a tool call in the previous turn, OpenAI/Claude sends `{"role": "assistant", "content": None, "tool_calls": [...]}`. Because `content` is `None`, `flatten_content` returns `""`, `if content:` fails, and the message is completely omitted.
3. **Tool Result Turn Orphanage:**
   A tool result message `{"role": "tool", "content": "...", "tool_call_id": "call_123"}` becomes `TOOL: ...` without indicating which tool was called, creating ambiguous conversational context.
4. **Prompt Termination Flaw (R3 Trigger):**
   If the prompt ends with a system hook (e.g. `UserPromptSubmit hook additional context: CAVEMAN MODE ACTIVE...`), the concatenated string terminates on `SYSTEM: ...`. Completion models interpret this as a missing user query and emit empty strings (`""`), triggering Claude Code's infinite synthetic recovery loop.

---

## 3. Upstream Antigravity Language Server Protocol Analysis

### 3.1 Protobuf Binary Inspection
Forensic inspection of the Language Server binary `/Applications/Antigravity.app/Contents/Resources/bin/language_server` confirms the protobuf descriptors for the completion service:
```protobuf
message GetModelResponseRequest {
    string prompt = 1;
    .exa.codeium_common_pb.Model model = 2;
}

message GetModelResponseResponse {
    string response = 1;
}
```
There are no message fields for `tools`, `functions`, `tool_choice`, or streaming flags in `GetModelResponseRequest`. The RPC is strictly unary:
```python
def rpc(method, payload):
    info = ensure_ls()
    body = json.dumps(payload).encode("utf-8")
    rpc_request = request.Request(
        f"http://{info['address']}/exa.language_server_pb.LanguageServerService/{method}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-codeium-csrf-token": info["csrf_token"],
        },
    )
    with request.urlopen(rpc_request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))
```
The RPC call blocks until the entire output is generated and returns the completed text.

---

## 4. Empirical Validation: Live Model Response to In-Bridge Prompt Demarcation

We executed live empirical tests directly against the running Antigravity Language Server daemon (PID 84261) using `antigravity_bridge.rpc("GetModelResponse", ...)` to evaluate model behavior.

### 4.1 Test 1: Single Tool Call Generation
**Injected Prompt:**
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
**Upstream Response Received:**
```xml
<tool_call>
{"name": "Write", "arguments": {"path": "/tmp/hello.txt", "content": "Hello World"}}
</tool_call>
```
*Result:* Model adhered 100% to the demarcation tags and structured JSON arguments.

### 4.2 Test 2: Mixed Conversational Plan + Demarcated Tool Call
**Injected Prompt:**
```text
SYSTEM: You have access to tools Bash and Read.
...
USER: First list files in /tmp, then read /tmp/hello.txt. Explain your plan first.

ASSISTANT:
```
**Upstream Response Received:**
```text
Here is my plan:
1. **List files in `/tmp`**: Use the `Bash` tool to run `ls -la /tmp` to see all files and directories located in `/tmp`.
2. **Read `/tmp/hello.txt`**: Use the `Read` tool to inspect and display the contents of `/tmp/hello.txt`.

Starting with step 1:

<tool_call>
{"name": "Bash", "arguments": {"command": "ls -la /tmp"}}
</tool_call>
```
*Result:* The model successfully output conversational reasoning preceding the tool call block. A parser must extract conversational text into `content` while translating the demarcation block into `tool_calls`.

---

## 5. Architectural Design: R1 Tool Calling Emulation Engine

To implement R1 cleanly without breaking existing configurations, `antigravity_bridge.py` requires three core architectural modules:
1. **Tool Schema Prompt Injector & Message Normalizer**
2. **Bidirectional Demarcation Parser**
3. **OpenAI Response & SSE Stream Synthesizer**

```
+-----------------------------------------------------------------------------------+
|                            POST /v1/chat/completions                              |
|   Payload: {messages: [...], tools: [...], tool_choice: "auto", stream: true}     |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| 1. TOOL SCHEMA PROMPT INJECTOR & NORMALIZER                                        |
|    - Maps tools into structured JSON schema instructions                           |
|    - Normalizes multi-turn assistant tool_calls -> <tool_call> tags               |
|    - Normalizes tool results -> TOOL RESULT (<name>): <content>                   |
|    - Normalizes prompt termination: guarantees ASSISTANT: suffix                  |
+-----------------------------------------------------------------------------------+
                                          |
                                          v  (prompt, model)
+-----------------------------------------------------------------------------------+
| 2. LANGUAGE SERVER RPC: GetModelResponse                                          |
|    - exa.language_server_pb.LanguageServerService/GetModelResponse                |
+-----------------------------------------------------------------------------------+
                                          |
                                          v  raw text response
+-----------------------------------------------------------------------------------+
| 3. BIDIRECTIONAL DEMARCATION PARSER                                               |
|    - Extracts <tool_call>...</tool_call> blocks (handles markdown fences ```json) |
|    - Parses JSON arguments, generates UUID call_ids (call_...)                    |
|    - Separates conversational text vs tool actions                                |
+-----------------------------------------------------------------------------------+
                                          |
                   +----------------------+----------------------+
                   | (stream: true)                              | (stream: false)
                   v                                             v
+------------------------------------+        +-------------------------------------+
| 4A. OPENAI SSE STREAM SYNTHESIZER  |        | 4B. OPENAI JSON SYNTHESIZER         |
|     Chunk 1: delta.content (if any)|        |     message: {                      |
|     Chunk 2: delta.tool_calls [...]|        |       content: text or null,        |
|     Chunk 3: finish: "tool_calls"  |        |       tool_calls: [...],            |
|     Chunk 4: data: [DONE]          |        |       finish_reason: "tool_calls"   |
+------------------------------------+        |     }                               |
                   |                          +-------------------------------------+
                   v                                             |
+----------------------------------------------------------------+------------------+
| LiteLLM Router -> Anthropic Adapter -> Claude Code / Client                       |
+-----------------------------------------------------------------------------------+
```

---

## 6. Specification of Emulation Components

### 6.1 Tool Schema Injection Format
When `payload.get("tools")` contains function definitions:
```python
def format_tool_instructions(tools: list[dict], tool_choice: Any = "auto") -> str:
    if not tools:
        return ""

    tool_lines = []
    for tool in tools:
        func = tool.get("function") or tool
        name = func.get("name")
        desc = func.get("description", "")
        params = func.get("parameters", {})
        tool_lines.append(
            f"- Tool: `{name}`\n"
            f"  Description: {desc}\n"
            f"  Parameters Schema: {json.dumps(params, ensure_ascii=False)}"
        )
    tools_str = "\n".join(tool_lines)

    instruction = (
        "# Available Tools\n"
        "You have access to the following tools:\n"
        f"{tools_str}\n\n"
        "# Tool Calling Instructions\n"
        "To invoke a tool, output a tool call block using the exact demarcation tags:\n"
        "<tool_call>\n"
        "{\"name\": \"TOOL_NAME\", \"arguments\": { ... }}\n"
        "</tool_call>\n\n"
        "Rules:\n"
        "1. The arguments must strictly conform to the parameter JSON schema.\n"
        "2. Do NOT wrap <tool_call> in markdown code blocks like ```json or ```xml.\n"
        "3. You may provide explanatory text before or after the tool call.\n"
        "4. You may make multiple tool calls by outputting multiple consecutive <tool_call>...</tool_call> blocks.\n"
        "5. If no tool call is needed, respond with normal conversational text."
    )

    if tool_choice == "required":
        instruction += "\nCRITICAL: You MUST call at least one tool in this turn."
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        req_name = tool_choice.get("function", {}).get("name")
        if req_name:
            instruction += f"\nCRITICAL: You MUST invoke the `{req_name}` tool in this turn."

    return instruction
```

### 6.2 Multi-Turn Message Normalization
In `messages_to_prompt`:
1. **Assistant Tool Calls:**
   ```python
   tool_calls = message.get("tool_calls") or []
   if tool_calls:
       tc_blocks = []
       for tc in tool_calls:
           fn = tc.get("function", {})
           name = fn.get("name", "")
           args = fn.get("arguments", "{}")
           if isinstance(args, str):
               try:
                   args = json.loads(args)
               except Exception:
                   pass
           tc_blocks.append(
               f"<tool_call>\n{json.dumps({'name': name, 'arguments': args}, ensure_ascii=False)}\n</tool_call>"
           )
       content_str = (content + "\n" + "\n".join(tc_blocks)).strip()
   ```
2. **Tool Results:**
   Track `tool_call_id -> tool_name` across earlier turns.
   ```python
   if role == "TOOL":
       call_id = message.get("tool_call_id") or ""
       tool_name = call_id_to_name.get(call_id, "")
       tag = f"TOOL RESULT ({tool_name})" if tool_name else f"TOOL RESULT ({call_id})" if call_id else "TOOL RESULT"
       parts.append(f"{tag}: {content}")
   ```
3. **Trailing Prompt Normalization:**
   Ensure prompt ends with:
   ```python
   prompt = prompt.rstrip()
   if not prompt.endswith("ASSISTANT:"):
       prompt += "\n\nASSISTANT:"
   ```

### 6.3 Demarcation Parser State Machine & Regex
```python
TOOL_CALL_REGEX = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)

def parse_model_response(content: str) -> tuple[str, list[dict]]:
    tool_calls = []
    
    def replacer(match):
        raw_inner = match.group(1).strip()
        # Strip potential markdown fences
        if raw_inner.startswith("```"):
            raw_inner = re.sub(r"^```(?:json|xml)?\s*", "", raw_inner)
            raw_inner = re.sub(r"\s*```$", "", raw_inner)
        try:
            parsed = json.loads(raw_inner)
            name = parsed.get("name")
            args = parsed.get("arguments", {})
            if isinstance(args, (dict, list)):
                args_str = json.dumps(args, ensure_ascii=False)
            elif isinstance(args, str):
                args_str = args
            else:
                args_str = "{}"
            
            if name:
                call_id = f"call_{uuid.uuid4().hex[:24]}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": args_str,
                    }
                })
        except Exception as e:
            log_service(f"Failed to parse tool_call JSON: {e} in {raw_inner!r}")
        return ""

    remaining_text = TOOL_CALL_REGEX.sub(replacer, content).strip()
    return remaining_text, tool_calls
```

---

## 7. Response Generation Protocols (Streaming vs Non-Streaming)

### 7.1 Non-Streaming Mode (`POST /v1/chat/completions`, `stream=False`)
When `tool_calls` are detected:
```json
{
  "id": "chatcmpl-ag-5ec91acea4654264a896c375331ce025",
  "object": "chat.completion",
  "created": 1791800500,
  "model": "antigravity-flash",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "I will inspect the workspace.\n",
        "tool_calls": [
          {
            "id": "call_53a9e62319f045bcae7102e1",
            "type": "function",
            "function": {
              "name": "Bash",
              "arguments": "{\"command\": \"ls -la\"}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
}
```
If no tool calls are detected:
- `finish_reason` is `"stop"`.
- `tool_calls` is omitted (or None).

### 7.2 Streaming Mode (`POST /v1/chat/completions`, `stream=True`)
LiteLLM's `_CombinedChunkSplitter` (`litellm/llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py`) enforces strict segregation of `content`, `tool_calls`, and `finish_reason` into distinct SSE frames.

**Chunk 1 (Text Content, emitted ONLY if remaining_text is non-empty):**
```text
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1791800500,"model":"antigravity-flash","choices":[{"index":0,"delta":{"role":"assistant","content":"I will inspect the workspace.\n"},"finish_reason":null}]}

```

**Chunk 2..N (Tool Call Deltas, one chunk per detected tool call):**
```text
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1791800500,"model":"antigravity-flash","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_53a9e62319f045bcae7102e1","type":"function","function":{"name":"Bash","arguments":"{\"command\": \"ls -la\"}"}}]},"finish_reason":null}]}

```

**Chunk Final (Finish Reason):**
```text
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1791800500,"model":"antigravity-flash","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}

```

**Stream Terminator:**
```text
data: [DONE]

```

---

## 8. Failure Modes & Edge Case Matrix

| Edge Case | Occurrence Scenario | Bridge Handling & Safeguard | Downstream Impact |
|---|---|---|---|
| **Empty Upstream Response (`{}` or `""`)** | Upstream LS times out or rejects prompt | **Return HTTP 502 Bad Gateway** immediately (R2). DO NOT return HTTP 200 with `""`. | LiteLLM triggers fallback deployment (`antigravity-remote-flash-medium`); Claude Code never sees 0-token empty output. |
| **Malformed Tool JSON** | Model hallucinates unquoted keys or truncates arguments | Catch `json.JSONDecodeError`, regex-extract fields, or fallback to treating `<tool_call>` content as conversational text in `delta.content`. | Client receives visible text explaining the action rather than HTTP 500 failure. |
| **Markdown Fences in Demarcation** | Model outputs `<tool_call>```json {...} ```</tool_call>` | Demarcation parser strips leading/trailing markdown code fences before parsing JSON. | Tool call successfully parsed and delivered. |
| **Multiple Parallel Tool Calls** | Model emits multiple consecutive `<tool_call>` blocks | Parser extracts all matches; emits sequential `tool_calls` chunks with incrementing `index` (0, 1, ...). | Downstream client executes parallel tool actions without truncation. |
| **Trailing System Hook (R3)** | Prompt ends with Caveman mode or statusline tags | Deduplicate system hooks and guarantee prompt terminates with `\n\nASSISTANT:`. | Eliminates prompt completion ambiguity and prevents empty outputs. |
| **Broken Pipe / Disconnect** | Client terminates early during streaming | Catch `BrokenPipeError` and `ConnectionResetError` gracefully, flush logs without crashing bridge thread. | Daemon remains responsive for concurrent and subsequent requests. |

---

## 9. Integration Verification Plan

1. **Unit Test Suite (`tests/test_tool_emulation.py`):**
   - Test `format_tool_instructions` with diverse OpenAI tool schemas (Write, Edit, Bash).
   - Test `parse_model_response` with:
     - Pure text
     - Pure single tool call
     - Text + single tool call
     - Multiple parallel tool calls
     - Markdown code fences inside `<tool_call>`
     - Malformed JSON resilience
   - Test `messages_to_prompt` multi-turn roundtrip preservation.
2. **Streaming Output Verification:**
   - Verify SSE output structure against LiteLLM Anthropic adapter.
   - Verify that chunks comply with `_CombinedChunkSplitter` group separation.
3. **End-to-End Live Replay (R4):**
   - Replay historical turns from `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl`.
   - Confirm Claude Code synthetic recovery loops (`[Your previous response had no visible output...]`) are 100% eliminated.
