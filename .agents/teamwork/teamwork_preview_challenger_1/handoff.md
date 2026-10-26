# Adversarial Stress Testing Handoff Report: Tool Calling Emulation & Response Parsing

**Agent**: Challenger 1 (critic, specialist)  
**Date**: 2026-10-26  
**Target File**: `service/bin/antigravity_bridge.py`  
**Test Suite**: `tests/test_adversarial_tool_calling.py`  
**Overall Risk Assessment**: **MEDIUM** (Core architecture is robust and passes all acceptance criteria; 6 specific edge-case failure modes were empirically isolated and proven).

---

## 1. Observation

### 1.1 Test Suite Execution via `uv`
The adversarial test harness was authored in `tests/test_adversarial_tool_calling.py` containing 39 unit, boundary, stress, and end-to-end HTTP tests.

```bash
cd /Users/thedawgctor/Desktop/litellmproxy-multiharness && VIRTUAL_ENV=.venv uv run python -m pytest tests/test_adversarial_tool_calling.py -v
```
**Result**:
```
============================== 39 passed in 1.70s ==============================
```

Full project test suite execution:
```bash
cd /Users/thedawgctor/Desktop/litellmproxy-multiharness && VIRTUAL_ENV=.venv uv run python -m pytest tests/test_session_replay.py tests/test_tool_choice_guard.py tests/test_config_validation.py tests/test_adversarial_tool_calling.py -v
```
**Result**:
```
============================= 103 passed in 11.71s =============================
```

### 1.2 Direct Observations of Implementation in `service/bin/antigravity_bridge.py`

#### Observation A: Greedy Regex in Fallback Argument Parser (Lines 129–139)
```python
129: name_m = re.search(r'["\']name["\']\s*:\s*["\']([^"\']+)["\']', raw_inner)
130: args_m = re.search(r'["\']arguments["\']\s*:\s*(\{.*\}|\[.*\])', raw_inner, re.DOTALL)
131: if name_m:
132:     parsed = {"name": name_m.group(1)}
133:     if args_m:
134:         try:
135:             parsed["arguments"] = json.loads(args_m.group(1))
136:         except Exception:
137:             parsed["arguments"] = args_m.group(1)
138:     else:
139:         parsed["arguments"] = {}
```
Empirical execution on `<tool_call>\n{"name": "test", "arguments": {"a": 1, "b": 2,}}\n</tool_call>`:
```python
# Output observed:
tcs[0]["function"]["arguments"] == '{"a": 1, "b": 2,}}'  # Contains extra closing brace '}}'
```
Empirical execution on `<tool_call>\n{"arguments": {"a": 1,}, "name": "test"}\n</tool_call>`:
```python
# Output observed:
tcs[0]["function"]["arguments"] == '{"a": 1,}, "name": "test"}'  # Greedily captures name field into arguments
```

#### Observation B: Regex Expectation Drops Stringified JSON Arguments (Line 130 & Line 139)
Empirical execution on `<tool_call>\n{"name": "Write", "arguments": "{"file_path": "/tmp/test"}"}\n</tool_call>`:
```python
# Output observed:
tcs[0]["function"]["arguments"] == '{}'  # Arguments dropped entirely to empty dict
```

#### Observation C: Strict Dict Typecheck Ignores Array of Tool Calls (Line 141)
```python
141: if isinstance(parsed, dict):
```
Empirical execution on `<tool_call>\n[{"name": "ToolA", "arguments": {}}]\n</tool_call>`:
```python
# Output observed:
len(tcs) == 0  # Tool call ignored, emitted as plaintext in remaining_text
```

#### Observation D: Multiple JSON Objects Inside One Demarcation Block (Line 127 & Line 130)
Empirical execution on `<tool_call>\n{"name": "ToolA", "arguments": {"x": 1}}\n{"name": "ToolB", "arguments": {"y": 2}}\n</tool_call>`:
```python
# Output observed:
len(tcs) == 1
tcs[0]["function"]["name"] == "ToolA"
tcs[0]["function"]["arguments"] == '{"x": 1}}\n{"name": "ToolB", "arguments": {"y": 2}}'  # ToolB swallowed
```

#### Observation E: Non-String `raw_response` in Bridge Handler (Line 547 & Line 115)
```python
547: remaining_text, tool_calls = parse_model_response(raw_response or "")
```
Empirical execution of `parse_model_response(12345)`:
```
TypeError: expected string or bytes-like object, got 'int'
```

#### Observation F: Non-String `role` in Incoming Messages (Line 209)
```python
209: role = (msg.get("role") or "user").lower()
```
Empirical execution of `messages_to_prompt([{"role": 12345, "content": "hi"}])`:
```
AttributeError: 'int' object has no attribute 'lower'
```

---

## 2. Logic Chain

1. **Robust Core Execution**:
   - `service/bin/antigravity_bridge.py` correctly handles standard compliant tool calling payloads.
   - When models emit standard `<tool_call>\n{"name": "...", "arguments": {...}}\n</tool_call>`, `json.loads` succeeds immediately without invoking regex fallbacks.
   - Markdown code fences (```json, ```xml, ```) wrapping or surrounding demarcations are cleanly stripped by lines 122–124 and line 171.
   - Multi-turn conversational history preserves tool calls and maps tool results (`TOOL RESULT (<tool_name>)`) accurately.
   - Large payloads (1MB+ strings, 10,000 array elements, depth 40 objects) parse in under 0.1s without catastrophic backtracking.
   - High-order Unicode (CJK, emojis, surrogate pairs, RTL scripts) is delivered byte-for-byte without double-encoding.

2. **Analysis of Vulnerability 1 (Greedy Regex Outer-Brace Capture)**:
   - When a tool call contains malformed JSON (e.g., trailing comma `{"a": 1,}`), `json.loads(raw_inner)` raises `json.decoder.JSONDecodeError`.
   - Control transfers to line 128 `except Exception:`.
   - Line 130 uses regex pattern `r'["\']arguments["\']\s*:\s*(\{.*\}|\[.*\])'` with `re.DOTALL`.
   - Because `.*` is greedy, it matches past the closing brace of the inner arguments object all the way to the closing brace of the containing tool_call JSON object.
   - When serialized, `tcs[0]["function"]["arguments"]` contains `{"a": 1,}}`.
   - **Impact**: Any downstream JSON parser attempting to parse `arguments` will fail with an unbalanced closing brace error.

3. **Analysis of Vulnerability 2 (Unescaped Outer-Quoted Arguments Dropped)**:
   - Completion models attempting to emit stringified JSON sometimes write `"arguments": "{"key": "val"}"` instead of `"arguments": "{\"key\": \"val\"}"`.
   - `json.loads` fails due to unescaped quotes.
   - Regex at line 130 strictly looks for `{` or `[` after `"arguments":\s*`.
   - Since the character following the colon is `"`, `args_m` evaluates to `None`.
   - Line 138-139 executes `else: parsed["arguments"] = {}`.
   - **Impact**: The arguments are completely discarded and replaced with empty dict `{}`. Tools requiring mandatory parameters (e.g. `Write`, `Edit`, `Bash`) fail immediately.

4. **Analysis of Vulnerability 3 (Single-Tag Array Neglect)**:
   - Some models emit multiple tool invocations as a JSON array within a single tag: `<tool_call>[{"name": "A"}, {"name": "B"}]</tool_call>`.
   - `json.loads` succeeds and returns a Python `list`.
   - Line 141 checks `if isinstance(parsed, dict):`. Since `parsed` is a `list`, the condition evaluates to `False`.
   - The replacer returns `match.group(0)` (the unparsed tag), and `tool_calls` remains empty.
   - **Impact**: Tool calls are not executed; downstream clients receive raw XML/demarcation text.

5. **Analysis of Vulnerability 4 (Multiple Objects in One Tag)**:
   - If a model emits two adjacent JSON objects in one tag, `json.loads` fails with `Extra data`.
   - Fallback `name_m` finds the first tool name.
   - Fallback `args_m` matches greedily to the end of the second tool call object.
   - **Impact**: Tool A receives corrupted arguments containing Tool B's schema, and Tool B is never executed.

6. **Analysis of Vulnerability 5 & 6 (Type Safety in Bridge Handler & Messages)**:
   - `parse_model_response` and `messages_to_prompt` assume inputs conform to string types.
   - Non-string `response` from mock/upstream or non-string `role` from client payloads trigger unhandled Python exceptions (`TypeError`, `AttributeError`), generating HTTP 500 instead of HTTP 400 or HTTP 502.

---

## 3. Caveats

1. **Upstream Protocol Compliance**: Upstream Antigravity Language Server protobuf `GetModelResponse` returns a string response in typical production usage. The `TypeError` on non-string response was observed only when deliberately passing an unexpected primitive type (e.g. int).
2. **System Prompt Guardrails**: The bridge's system prompt instructions explicitly instruct models to emit individual `<tool_call>` blocks rather than arrays or concatenated objects. Therefore, Vulnerabilities 3 and 4 only manifest when the upstream LLM deviates from the system instructions.
3. **Review-Only Constraint**: In strict adherence to challenger protocol, no modifications to `service/bin/antigravity_bridge.py` were made during this mission. All findings are verified by adversarial test harnesses in `tests/test_adversarial_tool_calling.py`.

---

## 4. Conclusion

- **Verdict**: The implementation in `service/bin/antigravity_bridge.py` is **ROBUST** for all standard operations, streaming SSE emissions, Caveman hook normalization, Unicode handling, and high-throughput payloads. All 15 existing project replay tests pass without regressions.
- **Identified Failure Modes**: 6 edge-case vulnerabilities exist in the fallback parser regexes and type coercion routines.
- **Actionable Recommendations for Hardening**:
  1. *Fix Fallback Regex Greedy Matching*: Replace greedy `\{.*\}` with balanced brace extraction or non-greedy `\{.*?\}` with recursive brace counting.
  2. *Handle Stringified JSON Arguments*: Support quotes after `arguments:` in fallback regex: `r'["\']arguments["\']\s*:\s*(?:["\'](\{.*\}|\[.*\])["\']|(\{.*\}|\[.*\]))'`.
  3. *Support Tool Call Arrays*: In line 141, add `elif isinstance(parsed, list): for item in parsed: ...` to extract batch tool calls.
  4. *Coerce Types Safely*: Ensure `raw_text = str(raw_text or "")` at the entry of `parse_model_response`, and `role = str(msg.get("role") or "user").lower()` in `messages_to_prompt`.

---

## 5. Verification Method

To independently reproduce all observations, challenges, and verified vulnerabilities, run:

```bash
# 1. Run the dedicated 39-test adversarial stress test suite:
cd /Users/thedawgctor/Desktop/litellmproxy-multiharness && VIRTUAL_ENV=.venv uv run python -m pytest tests/test_adversarial_tool_calling.py -v

# 2. Run the specific empirical vulnerability proofs:
cd /Users/thedawgctor/Desktop/litellmproxy-multiharness && VIRTUAL_ENV=.venv uv run python -m pytest tests/test_adversarial_tool_calling.py -k "test_vulnerability" -v

# 3. Run the full project test suite to verify zero regressions:
cd /Users/thedawgctor/Desktop/litellmproxy-multiharness && VIRTUAL_ENV=.venv uv run python -m pytest tests/test_session_replay.py tests/test_tool_choice_guard.py tests/test_config_validation.py tests/test_adversarial_tool_calling.py -v
```

### Invalidation Conditions
- A finding is invalidated if `tests/test_adversarial_tool_calling.py` fails to reproduce the observed outputs on Python 3.12 with `service/bin/antigravity_bridge.py`.
