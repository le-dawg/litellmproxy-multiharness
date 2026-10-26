# Survey Report: Error Handling, Fallback Circuit Breaker (R2) & Prompt Flattening Sanitization (R3)

**Author:** Explorer Survey 2  
**Date:** 2026-10-26  
**Working Directory:** `/Users/thedawgctor/Desktop/litellmproxy-multiharness/.agents/teamwork/teamwork_preview_explorer_survey_2`  
**Repository Root:** `/Users/thedawgctor/Desktop/litellmproxy-multiharness`  

---

## 1. Executive Summary

This survey provides the technical foundation for:
1. **Requirement R2:** Fail-fast upstream error handling in `service/bin/antigravity_bridge.py` and circuit breaker failover in `tool_choice_guard.py`.
2. **Requirement R3:** Multi-turn prompt flattening normalization, trailing system hook elimination, and conversational history deduplication (`CAVEMAN MODE ACTIVE` and `<!-- buddy: ... -->` statusline tags).

### Core Discoveries

| Investigation Area | Current Implementation Flaw | Severe Consequence | Required Hardening Fix |
|---|---|---|---|
| **Empty Response Handling** (`antigravity_bridge.py:294`) | `GetModelResponse` returning `{}` or `{"response": ""}` is caught as `content = ""` and returned as **HTTP 200 OK**. | Downstream client (Claude Code) receives 0 output tokens, assumes the model failed silently, and enters an infinite synthetic recovery loop: `[Your previous response had no visible output...]`. LiteLLM Router ignores it because status is 200. | Raise **HTTP 502 Bad Gateway** immediately when upstream output is empty/whitespace or `{}`. |
| **RPC & Timeout Handling** (`antigravity_bridge.py:372`) | Exceptions in `ensure_ls()` or `rpc()` (socket timeout, connection refused) are caught by generic `except Exception` and returned as **HTTP 500**. | Fails to distinguish between network timeouts (504) and gateway errors (502). | Return **HTTP 504 Gateway Timeout** on socket/urllib timeouts and **HTTP 502 Bad Gateway** on connection refused/RPC crash. |
| **Circuit Breaker Triggers** (`tool_choice_guard.py:565`) | `is_exhaustion_error(original_exception)` only checks for HTTP 429 and quota/credit exhaustion strings; it ignores HTTP 502 and 504. | When local Antigravity Language Server fails or times out, LiteLLM Router retries that single request via fallbacks, but `AntigravityCircuitBreaker` remains in `STATE_PRIMARY`. Every subsequent request still hits the dead local bridge, incurring repeated latency and retries. | Extend circuit breaker failure detection to trigger on **HTTP 502 and 504** upstream gateway failures, switching immediately to `STATE_FAILOVER` for remote routing. |
| **Terminal System Hooks** (`antigravity_bridge.py:61-75`) | `messages_to_prompt` renders messages in strict incoming order. Claude Code's `UserPromptSubmit` hooks inject `<system-reminder>` blocks (e.g. Caveman mode) after the user prompt. | Flattened prompt ends with `SYSTEM: <system-reminder> CAVEMAN MODE ACTIVE... Answer only what was asked...`. The completion model sees a system instruction with no trailing user prompt, concludes nothing was asked, and terminates with 0 tokens. | Re-order/normalize multi-turn messages so system reminders precede the user prompt, and guarantee the prompt **always terminates with an explicit user invocation** (`USER: ...`). |
| **Redundant History Bloat** (`7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl`) | Claude Code repeats the exact same `CAVEMAN MODE ACTIVE` reminder block (8x in 113 lines) on every turn, and assistant turns accumulate `<!-- buddy: ... -->` comments (11x). | Severe context bloat (1,500+ duplicate tokens) and noisy statusline comments polluting the model context window. | Filter duplicate historical caveman reminders (keep only the latest/initial active copy) and strip all `<!--\s*buddy:.*?-->` regex comments before prompt assembly. |

---

## 2. Deep Dive: Upstream Error Handling (`antigravity_bridge.py`)

### 2.1 The Current Code Path

In `service/bin/antigravity_bridge.py`:

```python
# Lines 284-306
upstream_payload = {"prompt": prompt, "model": internal_model}
log_traffic(
    "upstream_request",
    {
        "request_id": request_id,
        "method": "GetModelResponse",
        "payload": upstream_payload,
    },
)
upstream_response = rpc("GetModelResponse", upstream_payload)
content = upstream_response.get("response", "")
log_traffic(
    "upstream_response",
    {
        "request_id": request_id,
        "status": 200,
        "body": upstream_response,
    },
)
is_stream = bool(payload.get("stream"))
cmpl_id = f"chatcmpl-{uuid.uuid4().hex}"
created_ts = int(time.time())
```

If `upstream_response` is `{}` or `{"response": ""}`:
1. `content` is assigned `""`.
2. In streaming mode (lines 307–345):
   ```python
   chunk1 = {
       "id": cmpl_id,
       "object": "chat.completion.chunk",
       "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}],
   }
   chunk2 = {
       "id": cmpl_id,
       "object": "chat.completion.chunk",
       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
   }
   self.wfile.write(f"data: {json.dumps(chunk1)}\n\n".encode("utf-8"))
   self.wfile.write(f"data: {json.dumps(chunk2)}\n\n".encode("utf-8"))
   self.wfile.write(b"data: [DONE]\n\n")
   ```
   Status code sent: `self.send_response(200)`.
3. In non-streaming mode (lines 346–368):
   ```python
   response_body = {
       "id": cmpl_id,
       "object": "chat.completion",
       "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
       "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
   }
   self.respond_json(200, response_body)
   ```
   Status code sent: `self.respond_json(200, ...)`.

### 2.2 Why Returning HTTP 200 OK Breaks LiteLLM & Claude Code

1. **LiteLLM Router Perspective:**  
   LiteLLM only triggers router fallback deployments when an exception is raised by the provider client (e.g. `BadGatewayError`, `RateLimitError`, `APIConnectionError`, `InternalServerError`). When `antigravity_bridge.py` returns HTTP 200 OK, LiteLLM treats the HTTP request as completely successful. It does NOT inspect `choices[0].message.content` for emptiness. Fallback routing to `antigravity-remote-flash-medium` is completely bypassed.
2. **Circuit Breaker Perspective:**  
   `StripOrphanToolChoiceGuard.async_post_call_success_hook` receives the 200 OK response and calls `circuit_breaker.record_success()`. The circuit breaker is told that the primary deployment is healthy!
3. **Claude Code Client Perspective:**  
   Claude Code receives an empty assistant message with `content: ""` and no tool calls. Claude Code assumes an internal generation glitch occurred and sends its built-in recovery prompt:
   ```json
   {"role": "user", "content": "[Your previous response had no visible output. Please continue and produce a user-visible response.]"}
   ```
   Because the underlying condition (empty response or terminal system hook) remains unchanged, the next turn produces `""` again, trapping Claude Code in an unrecoverable 0-token loop.

### 2.3 Exception & Timeout Handling in `rpc()`

In `service/bin/antigravity_bridge.py`:
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

And lines 369–375:
```python
except error.HTTPError as exc:
    body = exc.read().decode("utf-8", "replace")
    self.respond_json(exc.code, {"error": {"message": body}})
except Exception as exc:
    log_service(f"bridge error: {exc}")
    self.respond_json(500, {"error": {"message": str(exc)}})
```

**Identified Issues:**
- `request.urlopen(..., timeout=90)` raises `urllib.error.URLError` wrapping `socket.timeout` or `TimeoutError` when upstream hangs. This falls into `except Exception`, returning HTTP 500 instead of HTTP 504 (Gateway Timeout).
- When the Antigravity Language Server crashes or closes the port, `ConnectionRefusedError` / `BrokenPipeError` is raised. This falls into `except Exception`, returning HTTP 500 instead of HTTP 502 (Bad Gateway).
- When `ensure_ls()` fails after 30 seconds, `RuntimeError("Antigravity language server did not become ready")` is raised. This returns HTTP 500 instead of HTTP 502.

### 2.4 Required Fail-Fast Bridge Architecture

1. **Empty Output Validation:**
   Immediately after `upstream_response = rpc("GetModelResponse", upstream_payload)`:
   ```python
   raw_response = upstream_response.get("response") if isinstance(upstream_response, dict) else None
   if not raw_response or (isinstance(raw_response, str) and not raw_response.strip()):
       log_service(f"GetModelResponse returned empty response for request {request_id}")
       self.respond_json(502, {
           "error": {
               "message": "Upstream Antigravity language server returned empty response",
               "type": "bad_gateway",
               "code": 502,
           }
       })
       return
   ```
2. **Explicit 504 Timeout Mapping:**
   Catch `(TimeoutError, socket.timeout)` or `urllib.error.URLError` with timeout:
   ```python
   except (TimeoutError, socket.timeout):
       self.respond_json(504, {
           "error": {
               "message": "Gateway Timeout: upstream language server did not respond in time",
               "type": "gateway_timeout",
               "code": 504,
           }
       })
   except error.URLError as exc:
       if isinstance(exc.reason, (socket.timeout, TimeoutError)):
           self.respond_json(504, {"error": {"message": f"Gateway Timeout: {exc.reason}", "type": "gateway_timeout", "code": 504}})
       else:
           self.respond_json(502, {"error": {"message": f"Bad Gateway: connection failed ({exc.reason})", "type": "bad_gateway", "code": 502}})
   except RuntimeError as exc:
       # e.g. ensure_ls failure
       self.respond_json(502, {"error": {"message": f"Bad Gateway: {exc}", "type": "bad_gateway", "code": 502}})
   ```

---

## 3. Deep Dive: LiteLLM Router & Circuit Breaker (`tool_choice_guard.py`)

### 3.1 LiteLLM Router Fallback Configuration

In `config/config.yaml`:
```yaml
router_settings:
  num_retries: 3
  retry_after: 3
  allowed_fails_policy:
    RateLimitError: 5
  fallbacks:
    - antigravity-flash-low: ["antigravity-remote-flash-low"]
    - antigravity-flash-medium: ["antigravity-remote-flash-medium"]
    - antigravity-flash-high: ["antigravity-remote-flash-high"]
    - antigravity-opus-4.6: ["antigravity-remote-opus-4.6"]
    - antigravity-flash: ["antigravity-remote-flash-medium"]
    - antigravity-pro: ["antigravity-remote-opus-4.6"]
```

And active deployments:
- Primary deployments point to `http://127.0.0.1:8788/v1` (`api_key: sk-antigravity-local`).
- Remote failover deployments point to `http://10.147.17.130:8788/v1` (`api_key: sk-antigravity-local`).

**How LiteLLM Router Operates on 502/504:**
When LiteLLM makes an OpenAI-compatible HTTP call to `http://127.0.0.1:8788/v1/chat/completions`:
- If status is 502, httpx raises `openai.APIStatusError` (status_code 502), which LiteLLM maps to `litellm.BadGatewayError`.
- LiteLLM's `Router` catches `litellm.BadGatewayError`.
- Under `router_settings.fallbacks`, LiteLLM matches `antigravity-flash` -> `antigravity-remote-flash-medium`.
- LiteLLM immediately dispatches the fallback call to `http://10.147.17.130:8788/v1`.
- The user request succeeds without returning an error to Claude Code!

### 3.2 The Circuit Breaker Mechanism in `tool_choice_guard.py`

In `tool_choice_guard.py`:
- `AntigravityCircuitBreaker` maintains three states:
  - `STATE_PRIMARY` ("primary"): Routes to local model `antigravity-flash-*`.
  - `STATE_FAILOVER` ("failover"): Rewrites model target to `antigravity-remote-flash-*` in `async_pre_call_hook`.
  - `STATE_PROBING` ("probing"): Cooldown (default 900s) has elapsed; routes 1 probe request to primary.
- State is persisted to `~/.litellm/failover_state.json`.

### 3.3 The Failure Detection Gap in `StripOrphanToolChoiceGuard`

In `tool_choice_guard.py`:
```python
# Lines 556-572
async def async_post_call_failure_hook(
    self,
    request_data: dict,
    original_exception: Exception,
    user_api_key_dict: Any,
    traceback_str: Optional[str] = None,
    *args: Any,
    **kwargs: Any,
) -> Optional[Any]:
    if original_exception is None:
        return None

    # Ignore failures from remote fallback models
    model = (request_data.get("model") or "") if isinstance(request_data, dict) else ""
    if "antigravity-remote" in model:
        return None

    if is_exhaustion_error(original_exception):
        self.circuit_breaker.record_failure(original_exception)
        verbose_proxy_logger.warning(...)
    return None
```

Look at `is_exhaustion_error(error)` (lines 35–110):
```python
status_code = getattr(error, "status_code", None)
if status_code in (429, "429"): return True
...
for phrase in EXHAUSTION_PHRASES: # "resource_exhausted", "quota exceeded", etc.
    if phrase in text_lower: return True
...
```

**The Gap:**
`is_exhaustion_error` only checks 429 and quota exhaustion!
When `antigravity_bridge.py` returns 502 Bad Gateway or 504 Gateway Timeout:
1. `is_exhaustion_error(original_exception)` returns `False`!
2. `self.circuit_breaker.record_failure(...)` is NOT called!
3. The circuit breaker remains in `STATE_PRIMARY`.
4. As a result: On the NEXT user request, `async_pre_call_hook` continues to route to the dead local bridge `antigravity-flash-high`!
5. The next request has to wait for local bridge timeout/failure again before LiteLLM Router retries remote.

**Required Hardening:**
Create a unified failure detection function `is_failover_error(error)` (or update `record_failure` and `is_exhaustion_error`):
```python
def is_failover_error(error: Any) -> bool:
    """Return True if error matches quota exhaustion (429) OR upstream gateway failure (502, 504)."""
    if is_exhaustion_error(error):
        return True
    if error is None:
        return False
    status_code = getattr(error, "status_code", None) or getattr(error, "http_status", None) or getattr(error, "code", None)
    if status_code in (502, 504, "502", "504"):
        return True
    if isinstance(error, dict):
        if error.get("status_code") in (502, 504, "502", "504") or error.get("code") in (502, 504, "502", "504"):
            return True
    text_lower = str(error).lower()
    if any(phrase in text_lower for phrase in ("502 bad gateway", "504 gateway timeout", "bad gateway", "gateway timeout")):
        return True
    return False
```
In `async_post_call_failure_hook`:
```python
if is_failover_error(original_exception):
    self.circuit_breaker.record_failure(original_exception)
```
And in `AntigravityCircuitBreaker.record_failure`:
```python
def record_failure(self, error: Any = None) -> bool:
    if error is not None and not is_failover_error(error):
        return False
    self._state = STATE_FAILOVER
    self._last_failure_time = self._time_fn()
    self._save_state(error)
    return True
```
This ensures that upon encountering 502 or 504 from the local bridge, the circuit breaker trips into `STATE_FAILOVER`, immediately routing all subsequent requests directly to remote deployments without stalling on local retries.

---

## 4. Deep Dive: Prompt Flattening & Terminal System Hooks (`messages_to_prompt`)

### 4.1 How `messages_to_prompt` Operates

In `service/bin/antigravity_bridge.py`:
```python
# Lines 61-75
def messages_to_prompt(messages):
    if not isinstance(messages, list):
        return flatten_content(messages)
    if len(messages) == 1 and isinstance(messages[0], dict):
        return flatten_content(messages[0].get("content"))
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

### 4.2 The Terminal Hook Problem Discovered in Session `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93`

In Claude Code session `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl`:
At turn 89–92:
- Line 89: User submits: `"bro wtf get to fecking work bro"`
- Line 90: System attachment: Plan mode reminder:
  `<system-reminder>\nPlan mode still active...\n</system-reminder>` (role: `system`)
- Line 91: System attachment: UserPromptSubmit hook:
  `<system-reminder>\nUserPromptSubmit hook additional context: CAVEMAN MODE ACTIVE (caveman). Respond terse like smart caveman. All technical substance stay. Only fluff die. Answer only what was asked: no unrequested background, lists, examples, walkthroughs, or follow-up offers; give code, steps, or warnings when the task needs them. Security warnings, irreversible actions, multi-step order: normal prose. Technical terms, code, commands, paths, and errors stay exact.\n</system-reminder>` (role: `system`)

When `messages_to_prompt` flattens these turns:
```
USER: bro wtf get to fecking work bro

SYSTEM: <system-reminder>
Plan mode still active...
</system-reminder>

SYSTEM: <system-reminder>
UserPromptSubmit hook additional context: CAVEMAN MODE ACTIVE (caveman). Respond terse like smart caveman. All technical substance stay. Only fluff die. Answer only what was asked...
</system-reminder>
```

**Why the Upstream Completion Model Returns Empty Output:**
1. Antigravity Language Server uses `GetModelResponse` (`LanguageServerService`), which is a text completion endpoint.
2. In text completion, the model predicts the continuation of the given text.
3. The prompt concludes with:
   `Answer only what was asked: no unrequested background, lists, examples...`
4. Because the prompt ends with this system rule, **nothing was asked in that final block**.
5. The model evaluates: "In this turn, no query was asked. Following the directive 'Answer only what was asked', output is null."
6. The model completes turn with 0 output tokens (`{"response": ""}`).
7. Claude Code sees no visible output, leading to the loop.

### 4.3 Trailing Hook Normalization Architecture

To guarantee the completion model always knows what to do:
1. **Re-order Hook Attachments:**  
   Any `SYSTEM:` reminders that accompany the user's turn must be positioned *before* the active user message, or folded into the system context.
2. **Terminal Invocations Guarantee:**  
   The flattened prompt must **always conclude with an explicit user invocation** (`USER: ...`).
   If the conversation's final block is a `SYSTEM:` block (e.g. system instructions only), an explicit user prompt must be appended:
   `\n\nUSER: Please proceed with the above instructions.`
   If a user message was followed by system reminders:
   ```
   SYSTEM: <system-reminder>
   Plan mode still active...
   </system-reminder>

   SYSTEM: <system-reminder>
   UserPromptSubmit hook additional context: CAVEMAN MODE ACTIVE...
   </system-reminder>

   USER: bro wtf get to fecking work bro
   ```
   Now the prompt ends with the user's explicit command!
   The model reads the instructions first, then the user's command at the terminal position, and immediately generates the response following caveman rules!

---

## 5. Deep Dive: Deduplication of System Reminders & Statusline Tags

### 5.1 Evidence from Historical Session Replay

Forensic frequency analysis of `~/.claude/projects/-Users-thedawgctor-Desktop-tempfuk-session-finder/7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl`:
- Total lines: 113
- `CAVEMAN MODE ACTIVE` occurrences: **19** across 8 distinct user prompt turns
- `buddy:` statusline occurrences: **11** across assistant responses
- Repeated text block:
  ```text
  UserPromptSubmit hook additional context: CAVEMAN MODE ACTIVE (caveman). Respond terse like smart caveman. All technical substance stay. Only fluff die. Answer only what was asked: no unrequested background, lists, examples, walkthroughs, or follow-up offers; give code, steps, or warnings when the task needs them. Security warnings, irreversible actions, multi-step order: normal prose. Technical terms, code, commands, paths, and errors stay exact.
  ```
  Length: 405 characters (~85 tokens).
  Repeated 8 times = 680 redundant tokens in just 8 turns! In a 40-turn session, this wastes >3,400 tokens per prompt request.

- `<!-- buddy: ... -->` statusline comments:
  In assistant turns, statusline companion comments are inserted:
  ```html
  <!-- buddy: *narrows slitted eyes* speak plainly, mortal, before I roast this empty prompt -->
  <!-- buddy: *taps claws impatiently* give me a spec or a bug to hunt -->
  <!-- buddy: *snorts a plume of smoke* call me Darktooth if you want my attention, mortal -->
  <!-- buddy: *grumbles from the status line* a plant, he calls me -->
  ```
  These are ephemeral UI decorations meant only for the terminal status bar. Re-feeding them in conversation history pollutes the model's instruction following and wastes tokens.

### 5.2 Deduplication & Sanitization Rules

In `messages_to_prompt(messages)`:

1. **Buddy Tag Removal:**  
   Strip all HTML comments matching `<!--\s*buddy:.*?-->` with `re.DOTALL | re.IGNORECASE` from all message content.
2. **System Reminder Deduplication:**  
   When scanning the message history:
   - Identify repeated hook blocks such as `UserPromptSubmit hook additional context: CAVEMAN MODE ACTIVE`.
   - Keep only the **latest active occurrence** (or merge it into the system prompt).
   - In earlier historical turns, omit or replace identical system-reminder blocks so they do not accumulate in the context.
3. **Multi-Turn Message Normalization Algorithm:**
   ```python
   def sanitize_and_deduplicate_messages(messages):
       cleaned_messages = []
       last_caveman_idx = -1

       # 1. First pass: find the index of the latest CAVEMAN reminder
       for i, msg in enumerate(messages):
           if isinstance(msg, dict):
               content = flatten_content(msg.get("content"))
               if "CAVEMAN MODE ACTIVE" in content:
                   last_caveman_idx = i

       # 2. Second pass: clean buddy tags and drop historical duplicate caveman reminders
       for i, msg in enumerate(messages):
           if not isinstance(msg, dict):
               continue
           role = (msg.get("role") or "user").lower()
           raw_content = flatten_content(msg.get("content"))

           # Strip buddy tags
           cleaned = re.sub(r"<!--\s*buddy:.*?-->", "", raw_content, flags=re.DOTALL | re.IGNORECASE).strip()

           # Drop redundant historical caveman reminders
           if "CAVEMAN MODE ACTIVE" in cleaned and i != last_caveman_idx:
               # If the entire message was just this reminder, skip it
               if "UserPromptSubmit hook additional context: CAVEMAN MODE ACTIVE" in cleaned:
                   continue

           if cleaned:
               cleaned_messages.append({"role": role, "content": cleaned})

       return cleaned_messages
   ```

4. **Terminal User Invocation Reordering:**
   After sanitizing:
   - Separate trailing system messages that immediately follow the final user message.
   - Relocate those system messages to *precede* the final user message.
   - If no user message exists in the conversation at all, append `{"role": "user", "content": "Please proceed."}`.
   - Assemble parts with `{ROLE.upper()}: {content}`.
   - The resulting prompt string is guaranteed to terminate with `USER: <active user prompt>`!

---

## 6. Edge Cases & Component Interactions

### 6.1 Interaction with R1 (Tool Calling Emulation)
When Explorer 1 / Milestone 1 implements in-bridge tool calling:
- If a model responds with tool call demarcations (e.g. `<tool_call>{...}</tool_call>`), the bridge parses it into `choices[0].message.tool_calls` (or streaming deltas).
- **Edge case:** What if the model outputs `<tool_call>...</tool_call>` with empty plain text?
  - This is a VALID tool call! It is NOT an empty 0-token failure.
  - The check for empty response in R2 must verify that BOTH plain text content AND parsed tool calls are absent before declaring an empty response (HTTP 502).

### 6.2 Interaction with Single-Message Prompts
If `messages` contains only 1 message:
- Currently `antigravity_bridge.py` lines 64–65:
  `if len(messages) == 1 and isinstance(messages[0], dict): return flatten_content(messages[0].get("content"))`
- If that single message is a `user` message, it returns the user content directly.
- If that single message is a `system` message, returning only system content causes the completion model to wait for a user prompt!
- Normalization must ensure that if the sole message is `system`, an explicit `\n\nUSER: Please proceed.` is appended.

### 6.3 Interaction with Streaming Chunks
When `stream: true` is passed:
- In the current code, the bridge waits for `rpc("GetModelResponse")` synchronously, then sends 2 artificial SSE chunks (`chunk1` with content, `chunk2` with finish_reason `stop`).
- If `GetModelResponse` fails or is empty, the bridge has NOT sent any SSE headers yet!
- Therefore, the bridge can cleanly respond with HTTP 502/504 JSON headers without corrupting an already-opened SSE stream.

---

## 7. Concrete File Paths & Functions to Modify

| Component | Target File | Functions / Sections | Changes Required |
|---|---|---|---|
| **Error Handling & 502/504** | `service/bin/antigravity_bridge.py` | `rpc(method, payload)`, `Handler.do_POST` | Check for empty/whitespace response `{}` and return 502. Map socket/urllib timeouts to 504. Map connection refused to 502. |
| **Circuit Breaker Upstream Triggers** | `tool_choice_guard.py` | `is_exhaustion_error` -> `is_failover_error`, `AntigravityCircuitBreaker.record_failure`, `StripOrphanToolChoiceGuard.async_post_call_failure_hook` | Add HTTP 502 and 504 status codes and Bad Gateway / Gateway Timeout strings to failover detection so the circuit breaker trips to `STATE_FAILOVER`. |
| **Prompt Flattening Normalization** | `service/bin/antigravity_bridge.py` | `messages_to_prompt(messages)` | Sanitize buddy tags, deduplicate historical `CAVEMAN MODE ACTIVE` reminders, re-order trailing system hooks before final user turn, and guarantee terminal `USER:` block. |
| **Config Fallbacks** | `config/config.yaml` | `router_settings.fallbacks` | Verify and ensure all primary `antigravity-*` models have remote fallback counterparts. |
| **Unit & Integration Tests** | `tests/test_tool_choice_guard.py` & `tests/test_bridge_error_and_prompt.py` | New test suites | Add test cases verifying: 502/504 trips circuit breaker, empty response returns 502, prompt normalization always ends with `USER:`, buddy tags stripped, caveman reminders deduplicated. |

---

## 8. Independent Verification Method

All verification can be independently executed via `uv`:
```bash
# 1. Run existing test suite
cd /Users/thedawgctor/Desktop/litellmproxy-multiharness && uv run --with pytest --with pytest-asyncio --with pyyaml python3 -m pytest tests/test_config_validation.py tests/test_tool_choice_guard.py

# 2. Inspect historical session for verified turn numbers
uv run python3 -c '
with open("/Users/thedawgctor/.claude/projects/-Users-thedawgctor-Desktop-tempfuk-session-finder/7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl") as f:
    text = f.read()
print("CAVEMAN count:", text.count("CAVEMAN MODE ACTIVE"))
print("buddy count:", text.count("buddy:"))
'
```
