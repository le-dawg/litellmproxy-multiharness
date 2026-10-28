# Handoff Report: Explorer Survey 2 (R2 & R3)

**Agent:** Explorer Survey 2  
**Task:** Survey Error Handling & Fallback Circuit Breaker (R2) and Prompt Flattening Sanitization & Trailing Hook Normalization (R3)  
**Handoff Type:** Hard (Survey Phase Complete)  
**Date:** 2026-10-26  

---

## 1. Observation

1. **Empty Upstream Output Masked as HTTP 200 OK:**
   - In `service/bin/antigravity_bridge.py:293-294`:
     ```python
     upstream_response = rpc("GetModelResponse", upstream_payload)
     content = upstream_response.get("response", "")
     ```
     When `upstream_response` is `{}` or `{"response": ""}`, `content` becomes `""`.
   - In lines 308-344 (streaming) and lines 346-368 (non-streaming):
     `self.send_response(200)` / `self.respond_json(200, response_body)` returns HTTP 200 with `choices[0].message.content = ""` or delta `content: ""` and `finish_reason: "stop"`.
   - In `config/config.yaml:27-38`, LiteLLM Router fallbacks are defined (`antigravity-flash: ["antigravity-remote-flash-medium"]`), but LiteLLM only triggers router fallbacks on exceptions. Receiving HTTP 200 OK bypasses all fallback routing.

2. **Generic 500 on Bridge Timeouts and Connection Failures:**
   - In `service/bin/antigravity_bridge.py:369-375`:
     ```python
     except error.HTTPError as exc:
         body = exc.read().decode("utf-8", "replace")
         self.respond_json(exc.code, {"error": {"message": body}})
     except Exception as exc:
         log_service(f"bridge error: {exc}")
         self.respond_json(500, {"error": {"message": str(exc)}})
     ```
     Urllib socket timeouts (`TimeoutError`, `urllib.error.URLError`) and connection drops (`ConnectionRefusedError`) fall through to `except Exception` and return HTTP 500, rather than HTTP 504 (Gateway Timeout) or HTTP 502 (Bad Gateway).

3. **Circuit Breaker Ignores Upstream 502/504 Failures:**
   - In `tool_choice_guard.py:565`:
     ```python
     if is_exhaustion_error(original_exception):
         self.circuit_breaker.record_failure(original_exception)
     ```
   - In `tool_choice_guard.py:35-110`, `is_exhaustion_error()` inspects status code 429 and phrases in `EXHAUSTION_PHRASES` ("resource_exhausted", "quota exceeded", etc.). It returns `False` for HTTP 502 and 504.
   - When the local bridge fails with 502 or 504, `StripOrphanToolChoiceGuard` does not trip the circuit breaker into `STATE_FAILOVER`. While LiteLLM Router retries that single request via fallbacks, every subsequent request still attempts the dead local port (`127.0.0.1:8788`), incurring repeated latency.

4. **Terminal System Hook Blocks Cause 0-Token Completions:**
   - In historical Claude Code session `~/.claude/projects/-Users-thedawgctor-Desktop-tempfuk-session-finder/7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl`:
     - Line 89: User message `"bro wtf get to fecking work bro"`.
     - Lines 90–91: Attachments rendered as `role: "system"` containing `<system-reminder>Plan mode still active...</system-reminder>` and `<system-reminder>UserPromptSubmit hook additional context: CAVEMAN MODE ACTIVE (caveman). Respond terse like smart caveman. All technical substance stay. Only fluff die. Answer only what was asked...`.
     - Line 92: Assistant response generated `content: [{"type": "text", "text": ""}]` with 0 output tokens.
     - Line 93: Claude Code injected synthetic prompt `[Your previous response had no visible output. Please continue and produce a user-visible response.]`.
     - Line 94: Assistant response generated `text: ""` again (0 tokens).
   - In `service/bin/antigravity_bridge.py:61-75`:
     ```python
     def messages_to_prompt(messages):
         ...
         parts.append(f"{role}: {content}")
         return "\n\n".join(parts)
     ```
     `messages_to_prompt` renders messages in order. When trailing system hook blocks follow the user message, the prompt terminates with `SYSTEM: ... CAVEMAN MODE ACTIVE ... Answer only what was asked...`. The completion model sees this system directive at the tail, evaluates that no user question was asked in this final block, and completes with 0 tokens.

5. **Redundant History Context Bloat:**
   - In `7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl`:
     - The identical 85-token `CAVEMAN MODE ACTIVE` hook reminder is repeated 8 times (lines 18, 42, 50, 59, 67, 80, 91, 101).
     - Assistant messages carry statusline comments matching `<!--\s*buddy:.*?-->` 11 times across historical turns.
   - These comments and duplicate reminders re-enter the prompt history on every turn without serving any model generation purpose.

---

## 2. Logic Chain

1. **From Observation 1 to R2 Root Cause:**
   - Because `antigravity_bridge.py` wraps empty upstream strings in HTTP 200 OK, LiteLLM considers the request completed successfully.
   - LiteLLM does not trigger its router fallback deployments (`antigravity-flash -> antigravity-remote-flash-medium`) when it receives HTTP 200.
   - Claude Code receives an empty assistant response and enters its recovery loop `[Your previous response had no visible output...]`.
   - Therefore, `antigravity_bridge.py` must actively validate that `upstream_response.get("response")` is non-empty before returning 200. If empty, it must immediately return HTTP 502 Bad Gateway.

2. **From Observation 2 & 3 to Circuit Breaker R2 Hardening:**
   - LiteLLM Router intercepts 502/504 errors and dispatches fallbacks for individual turns.
   - However, `StripOrphanToolChoiceGuard.circuit_breaker` stays in `STATE_PRIMARY` because `is_exhaustion_error` rejects 502/504.
   - Expanding `is_exhaustion_error` (or introducing `is_failover_error`) to cover HTTP 502, HTTP 504, and upstream gateway timeout/connection errors allows `self.circuit_breaker.record_failure(...)` to trip the circuit breaker into `STATE_FAILOVER`.
   - In `STATE_FAILOVER`, `async_pre_call_hook` immediately rewrites `antigravity-*` to `antigravity-remote-*`, bypassing the failed local bridge for subsequent requests until the probe cooldown expires.

3. **From Observation 4 to R3 Prompt Flattening Normalization:**
   - Completion models generate text continuations based on the end of the prompt.
   - When `messages_to_prompt` blindly places system hooks at the end, the prompt ends with `SYSTEM: ... Answer only what was asked...`.
   - Reordering trailing system hooks so that they precede the active user turn, and ensuring the flattened prompt always ends with `USER: <active user prompt>` (or appending `USER: Please proceed.` when no user prompt exists), guarantees the model is explicitly prompted to generate an assistant answer.

4. **From Observation 5 to Context Sanitization:**
   - Historical turns do not require repeated copies of identical `CAVEMAN MODE ACTIVE` reminders. Retaining only the latest active reminder and stripping earlier duplicates saves hundreds of prompt tokens per turn.
   - `<!-- buddy: ... -->` comments are ephemeral UI annotations. Stripping them via regex `<!--\s*buddy:.*?-->` cleans up the conversation history and prevents instruction pollution.

---

## 3. Caveats

1. **Tool Call Delimiters Interaction (R1):**
   When tool call emulation (R1) is implemented, model output may contain `<tool_call>...</tool_call>` with empty conversational text. The 502 emptiness check in `antigravity_bridge.py` must verify that neither text content nor tool calls were produced before returning HTTP 502.
2. **Streaming Protocol:**
   Because `antigravity_bridge.py` calls `rpc("GetModelResponse")` before sending any HTTP headers or SSE data, returning HTTP 502 or 504 for empty responses or timeouts can be done cleanly with standard JSON error responses without needing SSE error events.
3. **Remote Deployment Reachability:**
   Fallback deployments in `config.yaml` route to `http://10.147.17.130:8788/v1`. During test replay in isolated environments (R4), mock endpoints or ephemeral fixtures must be used if the remote LAN IP is unreachable.

---

## 4. Conclusion

1. **R2 Solution:**
   - In `service/bin/antigravity_bridge.py`: If `upstream_response` is empty or `response` is empty/whitespace, raise HTTP 502 Bad Gateway. If `rpc` encounters socket/urllib timeouts, return HTTP 504 Gateway Timeout. If connection is refused or Language Server crashes, return HTTP 502 Bad Gateway.
   - In `tool_choice_guard.py`: Update `is_failover_error` to catch HTTP 502 and 504, triggering `circuit_breaker.record_failure(...)` to transition the circuit breaker to `STATE_FAILOVER`.
2. **R3 Solution:**
   - In `service/bin/antigravity_bridge.py`: Refactor `messages_to_prompt(messages)` to:
     1. Strip `<!--\s*buddy:.*?-->` regex comments from all message content.
     2. Deduplicate historical `CAVEMAN MODE ACTIVE` system-reminder blocks, keeping only the latest active turn's reminder.
     3. Reorder trailing system messages to precede the active user message so that the flattened prompt **always terminates with an explicit user invocation** (`USER: ...`).

---

## 5. Verification Method

Independent verification can be executed with `uv`:

```bash
# 1. Verify test suite passes with uv
cd /Users/thedawgctor/Desktop/litellmproxy-multiharness && uv run --with pytest --with pytest-asyncio --with pyyaml python3 -m pytest tests/test_config_validation.py tests/test_tool_choice_guard.py

# 2. Verify caveman and buddy counts in historical session
uv run python3 -c '
with open("/Users/thedawgctor/.claude/projects/-Users-thedawgctor-Desktop-tempfuk-session-finder/7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93.jsonl") as f:
    text = f.read()
print("CAVEMAN count:", text.count("CAVEMAN MODE ACTIVE"))
print("buddy count:", text.count("buddy:"))
'
```

**Invalidation Conditions:**
- If returning HTTP 502 on empty `{}` causes LiteLLM to raise an unhandled exception instead of triggering `router_settings.fallbacks`.
- If prompt reordering breaks messages that have no user prompt.
