"""Adversarial Stress-Testing Suite for Antigravity Bridge Tool Calling Emulation.

Systematically challenges:
1. Malformed JSON within <tool_call> (unescaped quotes, raw newlines, trailing commas, missing braces, single quotes, unquoted keys, non-dict args)
2. Markdown code fences and demarcations (```json, ```xml, ```, fences inside argument strings, fences outside tags)
3. Partial, unclosed, nested, uppercase, and malformed tags
4. Mixed turns: multiple tool calls interspersed with conversational text in streaming and non-streaming modes
5. Large payloads (1MB+, 10k items, depth 50) and schema complexity
6. Unicode, emojis, RTL, control codes, regex/sql/shell escapes
7. Multi-turn tool execution outputs (TOOL RESULT) containing demarcations, trailing hooks normalization
8. End-to-end EphemeralBridge HTTP stress verification
"""

from __future__ import annotations

import json
import socket
import time
import uuid
from typing import Any, Dict, List
import pytest
import httpx

from service.bin import antigravity_bridge as bridge
from tests.replay_harness import (
    EphemeralBridge,
    MockLanguageServer,
    build_chat_payload,
    parse_sse_events,
)


@pytest.fixture
def mock_ls():
    server = MockLanguageServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def ephemeral_bridge(mock_ls):
    bridge_inst = EphemeralBridge(mock_ls)
    bridge_inst.start()
    try:
        yield bridge_inst
    finally:
        bridge_inst.stop()


# ===================================================================== #
#  1. Malformed JSON in <tool_call>                                      #
# ===================================================================== #

class TestMalformedJsonToolCalling:
    """Stress tests parser against malformed and non-standard JSON in demarcation tags."""

    def test_unescaped_quotes_fallback(self):
        """Verify unescaped quotes inside string arguments are salvaged or handled gracefully."""
        raw = '<tool_call>\n{"name": "bash", "arguments": {"cmd": "echo "hello" world"}}\n</tool_call>'
        remaining, tcs = bridge.parse_model_response(raw)
        # Should not crash. Check whether tool call was salvaged or left in remaining text
        assert isinstance(tcs, list)
        if tcs:
            assert tcs[0]["function"]["name"] == "bash"
            assert "echo" in tcs[0]["function"]["arguments"]

    def test_raw_newlines_inside_json(self):
        """Verify unescaped literal raw newlines inside string values do not crash parser."""
        # 0x0A raw newline inside string is invalid JSON per RFC 8259
        raw = '<tool_call>\n{"name": "write", "arguments": {"content": "line1\nline2"}}\n</tool_call>'
        remaining, tcs = bridge.parse_model_response(raw)
        assert isinstance(tcs, list)
        if tcs:
            assert tcs[0]["function"]["name"] == "write"

    def test_trailing_comma_in_arguments(self):
        """Verify trailing commas in JSON object are tolerated via fallback regex."""
        raw = '<tool_call>\n{"name": "test_tool", "arguments": {"foo": "bar", "count": 42,}}\n</tool_call>'
        remaining, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 1
        assert tcs[0]["function"]["name"] == "test_tool"
        assert "foo" in tcs[0]["function"]["arguments"]

    def test_single_quoted_pseudo_json(self):
        """Verify single quotes instead of double quotes does not crash."""
        raw = "<tool_call>\n{'name': 'single_quote_tool', 'arguments': {'key': 'value'}}\n</tool_call>"
        remaining, tcs = bridge.parse_model_response(raw)
        assert isinstance(tcs, list)
        # Fallback regex searches for ["\']name["\']
        if tcs:
            assert tcs[0]["function"]["name"] == "single_quote_tool"

    def test_missing_closing_brace_in_arguments(self):
        """Verify missing closing brace in arguments does not crash parser."""
        raw = '<tool_call>\n{"name": "truncated_tool", "arguments": {"file": "/path/to/file"\n</tool_call>'
        remaining, tcs = bridge.parse_model_response(raw)
        assert isinstance(tcs, list)

    def test_primitive_arguments(self):
        """Verify primitive values for arguments (string, number, bool, null) are handled."""
        # String argument
        raw_str = '<tool_call>\n{"name": "str_tool", "arguments": "plain text"}\n</tool_call>'
        _, tcs_str = bridge.parse_model_response(raw_str)
        assert len(tcs_str) == 1
        assert tcs_str[0]["function"]["arguments"] == "plain text"

        # Integer argument
        raw_num = '<tool_call>\n{"name": "num_tool", "arguments": 12345}\n</tool_call>'
        _, tcs_num = bridge.parse_model_response(raw_num)
        assert len(tcs_num) == 1
        assert tcs_num[0]["function"]["arguments"] == "{}"

        # Null argument
        raw_null = '<tool_call>\n{"name": "null_tool", "arguments": null}\n</tool_call>'
        _, tcs_null = bridge.parse_model_response(raw_null)
        assert len(tcs_null) == 1
        assert tcs_null[0]["function"]["arguments"] == "{}"

    def test_parameters_alias_and_function_wrapper(self):
        """Verify support for 'parameters' key and 'function' wrapper schema."""
        raw_params = '<tool_call>\n{"name": "param_tool", "parameters": {"limit": 10}}\n</tool_call>'
        _, tcs_params = bridge.parse_model_response(raw_params)
        assert len(tcs_params) == 1
        assert json.loads(tcs_params[0]["function"]["arguments"]) == {"limit": 10}

        raw_func = '<tool_call>\n{"function": {"name": "func_tool", "arguments": {"x": 1}}}\n</tool_call>'
        _, tcs_func = bridge.parse_model_response(raw_func)
        assert len(tcs_func) == 1
        assert tcs_func[0]["function"]["name"] == "func_tool"

    def test_missing_name_returns_raw_text(self):
        """Verify demarcation block with missing name is not lost and left in text."""
        raw = '<tool_call>\n{"arguments": {"foo": "bar"}}\n</tool_call>'
        remaining, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 0
        assert "<tool_call>" in remaining


# ===================================================================== #
#  2. Markdown Code Fences inside and outside Demarcation Tags          #
# ===================================================================== #

class TestMarkdownCodeFences:
    """Stress tests markdown code fence combinations."""

    def test_fenced_with_json_and_xml(self):
        """Verify ```json and ```xml code fences inside <tool_call> are stripped."""
        raw_json = '<tool_call>\n```json\n{"name": "test_json", "arguments": {"a": 1}}\n```\n</tool_call>'
        _, tcs_json = bridge.parse_model_response(raw_json)
        assert len(tcs_json) == 1
        assert tcs_json[0]["function"]["name"] == "test_json"

        raw_xml = '<tool_call>\n```xml\n{"name": "test_xml", "arguments": {"b": 2}}\n```\n</tool_call>'
        _, tcs_xml = bridge.parse_model_response(raw_xml)
        assert len(tcs_xml) == 1
        assert tcs_xml[0]["function"]["name"] == "test_xml"

    def test_fenced_with_generic_backticks(self):
        """Verify generic ``` fence without language tag is stripped."""
        raw = '<tool_call>\n```\n{"name": "test_generic", "arguments": {}}\n```\n</tool_call>'
        _, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 1
        assert tcs[0]["function"]["name"] == "test_generic"

    def test_code_fences_outside_tool_call(self):
        """Verify code fences wrapping the entire <tool_call> tag are handled cleanly."""
        raw = '```json\n<tool_call>\n{"name": "outer_fenced", "arguments": {"k": "v"}}\n</tool_call>\n```'
        remaining, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 1
        assert tcs[0]["function"]["name"] == "outer_fenced"
        # Leftover empty code fences should be stripped
        assert remaining == ""

    def test_code_blocks_inside_arguments_content(self):
        """Verify that markdown code blocks INSIDE string arguments are preserved intact."""
        code_snippet = "```python\ndef hello():\n    return 'world'\n```"
        payload = {"name": "Write", "arguments": {"file_path": "/tmp/test.py", "content": code_snippet}}
        raw = f"<tool_call>\n{json.dumps(payload)}\n</tool_call>"
        remaining, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 1
        args = json.loads(tcs[0]["function"]["arguments"])
        assert args["content"] == code_snippet


# ===================================================================== #
#  3. Partial, Unclosed, and Nested Demarcation Tags                   #
# ===================================================================== #

class TestPartialAndNestedDemarcations:
    """Stress tests boundary cases with unclosed and nested tags."""

    def test_unclosed_opening_tag(self):
        """Verify model output cut off before </tool_call> returns raw text without crash."""
        raw = 'Generating tool call:\n<tool_call>\n{"name": "unclosed_tool", "arguments": {"x": 1}'
        remaining, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 0
        assert "<tool_call>" in remaining
        assert "unclosed_tool" in remaining

    def test_missing_opening_tag(self):
        """Verify closing tag without opening tag is treated as text without crash."""
        raw = '{"name": "missing_open", "arguments": {}}\n</tool_call>'
        remaining, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 0
        assert "</tool_call>" in remaining

    def test_empty_and_whitespace_tags(self):
        """Verify empty and whitespace-only <tool_call></tool_call> tags do not crash."""
        raw1 = "<tool_call></tool_call>"
        rem1, tcs1 = bridge.parse_model_response(raw1)
        assert len(tcs1) == 0

        raw2 = "<tool_call>   \n\t   </tool_call>"
        rem2, tcs2 = bridge.parse_model_response(raw2)
        assert len(tcs2) == 0

    def test_nested_tags(self):
        """Verify nested <tool_call><tool_call> tags do not hang or crash."""
        raw = '<tool_call><tool_call>\n{"name": "nested_tool", "arguments": {"a": 1}}\n</tool_call></tool_call>'
        remaining, tcs = bridge.parse_model_response(raw)
        assert isinstance(tcs, list)
        # Should extract tool call or leave text safely
        if tcs:
            assert tcs[0]["function"]["name"] == "nested_tool"

    def test_case_insensitive_demarcation(self):
        """Verify <TOOL_CALL> and <Tool_Call> tags are matched."""
        raw = '<TOOL_CALL>\n{"name": "upper_tool", "arguments": {}}\n</TOOL_CALL>'
        _, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 1
        assert tcs[0]["function"]["name"] == "upper_tool"

    def test_adjacent_demarcation_tags_without_spacing(self):
        """Verify two tool calls with no spacing between tags are both parsed."""
        raw = (
            '<tool_call>{"name": "tool1", "arguments": {"id": 1}}</tool_call>'
            '<tool_call>{"name": "tool2", "arguments": {"id": 2}}</tool_call>'
        )
        remaining, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 2
        assert tcs[0]["function"]["name"] == "tool1"
        assert tcs[1]["function"]["name"] == "tool2"
        assert remaining == ""


# ===================================================================== #
#  4. Mixed Turns & Multiple Tool Calls Interspersed with Text         #
# ===================================================================== #

class TestMixedTurnsAndMultipleToolCalls:
    """Stress tests mixed conversational text with multiple tool calls."""

    def test_text_before_between_and_after(self):
        """Verify conversational text before, between, and after tool calls is preserved."""
        raw = (
            "I will execute two tools to fulfill your request.\n"
            "<tool_call>\n"
            '{"name": "ReadFile", "arguments": {"path": "/etc/hosts"}}\n'
            "</tool_call>\n"
            "Now checking network status:\n"
            "<tool_call>\n"
            '{"name": "Ping", "arguments": {"host": "127.0.0.1"}}\n'
            "</tool_call>\n"
            "Execution completed."
        )
        remaining, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 2
        assert tcs[0]["function"]["name"] == "ReadFile"
        assert tcs[1]["function"]["name"] == "Ping"
        assert "I will execute two tools" in remaining
        assert "Now checking network status:" in remaining
        assert "Execution completed." in remaining

    def test_three_consecutive_tool_calls_no_text(self):
        """Verify consecutive tool calls with empty remaining text."""
        raw = (
            '<tool_call>\n{"name": "t1", "arguments": {}}\n</tool_call>\n'
            '<tool_call>\n{"name": "t2", "arguments": {}}\n</tool_call>\n'
            '<tool_call>\n{"name": "t3", "arguments": {}}\n</tool_call>'
        )
        remaining, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 3
        assert remaining == ""
        assert [t["function"]["name"] for t in tcs] == ["t1", "t2", "t3"]


# ===================================================================== #
#  5. Extremely Large Payloads & Deep Nesting & Schema Complexity       #
# ===================================================================== #

class TestLargePayloadsAndSchemaComplexity:
    """Stress tests large payloads, deep nesting, and large schemas."""

    def test_large_tool_argument_payload_1mb(self):
        """Verify 1MB tool argument payload does not crash or cause catastrophic backtracking."""
        large_string = "A" * (1024 * 1024)  # 1 MB
        raw = f'<tool_call>\n{{"name": "BigData", "arguments": {{"payload": "{large_string}"}}}}\n</tool_call>'
        start_time = time.time()
        remaining, tcs = bridge.parse_model_response(raw)
        elapsed = time.time() - start_time

        assert elapsed < 5.0, f"Parsing took too long ({elapsed:.2f}s) - potential regex catastrophe"
        assert len(tcs) == 1
        assert tcs[0]["function"]["name"] == "BigData"
        args = json.loads(tcs[0]["function"]["arguments"])
        assert len(args["payload"]) == 1024 * 1024

    def test_large_array_payload_10k_items(self):
        """Verify tool call with 10,000 array elements is parsed efficiently."""
        items = list(range(10000))
        raw = f'<tool_call>\n{{"name": "BatchProcess", "arguments": {{"items": {json.dumps(items)}}}}}\n</tool_call>'
        start_time = time.time()
        remaining, tcs = bridge.parse_model_response(raw)
        elapsed = time.time() - start_time

        assert elapsed < 3.0, f"Parsing took {elapsed:.2f}s"
        assert len(tcs) == 1
        args = json.loads(tcs[0]["function"]["arguments"])
        assert len(args["items"]) == 10000

    def test_deeply_nested_json_object(self):
        """Verify deeply nested JSON structure (depth 40) is parsed without RecursionError."""
        nested: Dict[str, Any] = {"leaf": "value"}
        for i in range(40):
            nested = {f"level_{i}": nested}
        raw = f'<tool_call>\n{{"name": "DeepTree", "arguments": {json.dumps(nested)}}}\n</tool_call>'
        remaining, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 1
        assert tcs[0]["function"]["name"] == "DeepTree"

    def test_format_tool_instructions_with_many_complex_schemas(self):
        """Verify format_tool_instructions scales to 50 tools with complex JSON schemas."""
        tools = []
        for i in range(50):
            tools.append({
                "type": "function",
                "function": {
                    "name": f"tool_operation_{i}",
                    "description": f"Perform operation {i} with complex validation schema.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "pattern": "^[a-z0-9_-]+$"},
                            "count": {"type": "integer", "minimum": 0, "maximum": 1000},
                            "tags": {"type": "array", "items": {"type": "string"}},
                            "options": {
                                "type": "object",
                                "properties": {
                                    "verbose": {"type": "boolean"},
                                    "mode": {"type": "string", "enum": ["fast", "safe", "strict"]},
                                },
                            },
                        },
                        "required": ["id"],
                    },
                },
            })

        inst = bridge.format_tool_instructions(tools, tool_choice="auto")
        assert len(inst) > 1000
        assert "tool_operation_0" in inst
        assert "tool_operation_49" in inst
        assert "# Available Tools" in inst
        assert "<tool_call>" in inst


# ===================================================================== #
#  6. Unicode, Special Characters, and Control Sequences               #
# ===================================================================== #

class TestUnicodeAndSpecialCharacters:
    """Stress tests Unicode, emoji, RTL, control codes, and code escapes."""

    def test_cjk_and_multilingual_unicode(self):
        """Verify CJK and international characters are preserved without corruption."""
        cjk_text = "日本語テスト, 简体中文测试, 번역 테스트, Übergrößenträger, déjà vu"
        raw = f'<tool_call>\n{{"name": "translate", "arguments": {{"text": "{cjk_text}"}}}}\n</tool_call>'
        _, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 1
        args = json.loads(tcs[0]["function"]["arguments"])
        assert args["text"] == cjk_text

    def test_emojis_and_surrogate_pairs(self):
        """Verify high-order Unicode emojis (including zero-width joiners) are preserved."""
        emojis = "🔥🚀👨‍👩‍👧‍👦🎉🦄🧙‍♂️🌈"
        raw = f'<tool_call>\n{{"name": "emoji_tool", "arguments": {{"icons": "{emojis}"}}}}\n</tool_call>'
        _, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 1
        args = json.loads(tcs[0]["function"]["arguments"])
        assert args["icons"] == emojis

    def test_rtl_arabic_and_hebrew(self):
        """Verify Right-to-Left scripts (Arabic and Hebrew) are preserved."""
        rtl_text = "مرحبا بالعالم - שלום עולם"
        raw = f'<tool_call>\n{{"name": "rtl_tool", "arguments": {{"msg": "{rtl_text}"}}}}\n</tool_call>'
        _, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 1
        args = json.loads(tcs[0]["function"]["arguments"])
        assert args["msg"] == rtl_text

    def test_sql_and_shell_metacharacters(self):
        """Verify SQL injection payloads and shell metacharacters in arguments are safely handled."""
        payload = {
            "sql": "SELECT * FROM users WHERE name = 'O''Reilly' UNION SELECT password FROM admin; --",
            "cmd": "rm -rf /tmp/test; cat <(echo 'malicious') | grep -v 'safe' && echo $PATH",
        }
        raw = f"<tool_call>\n{json.dumps({'name': 'safe_eval', 'arguments': payload})}\n</tool_call>"
        _, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 1
        args = json.loads(tcs[0]["function"]["arguments"])
        assert args["sql"] == payload["sql"]
        assert args["cmd"] == payload["cmd"]


# ===================================================================== #
#  7. Multi-Turn Tool History Preservation & Demarcation in Tool Outputs#
# ===================================================================== #

class TestMultiTurnToolExecutionHistory:
    """Stress tests multi-turn prompt flattening, tool outputs, and hook ordering."""

    def test_tool_result_containing_literal_tool_call_demarcation(self):
        """Verify that if a tool execution output contains <tool_call> tags, prompt flattening does not fail."""
        # e.g., grep or cat output containing <tool_call>
        demarcation_output = (
            "Found matching code:\n"
            "<tool_call>\n"
            '{"name": "SubTool", "arguments": {"arg": 1}}\n'
            "</tool_call>\n"
            "End of file."
        )
        messages = [
            {"role": "user", "content": "Search for tool calls in the repo."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_grep_1",
                        "type": "function",
                        "function": {"name": "Grep", "arguments": '{"pattern": "<tool_call>"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_grep_1",
                "content": demarcation_output,
            },
            {"role": "user", "content": "What did you find?"},
        ]

        prompt = bridge.messages_to_prompt(messages)
        assert "TOOL RESULT (Grep):" in prompt
        assert "Found matching code:" in prompt
        assert prompt.rstrip().endswith("ASSISTANT:")

    def test_trailing_caveman_hook_after_tool_result(self):
        """Verify that when a trailing hook arrives after a tool result, the prompt terminates correctly."""
        messages = [
            {"role": "user", "content": "write a file"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_write_1",
                        "type": "function",
                        "function": {"name": "Write", "arguments": '{"path": "x.txt"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_write_1",
                "content": "File written successfully.",
            },
            {
                "role": "system",
                "content": "CAVEMAN MODE ACTIVE (caveman). Respond terse like smart caveman.",
            },
        ]

        prompt = bridge.messages_to_prompt(messages)
        # Trailing hook must NOT end the prompt with SYSTEM:
        assert not prompt.rstrip().endswith("SYSTEM:")
        # Prompt must end with ASSISTANT: cue
        assert prompt.rstrip().endswith("ASSISTANT:")


# ===================================================================== #
#  8. End-to-End EphemeralBridge HTTP Stress Verification              #
# ===================================================================== #

class TestEndToEndBridgeHttpStress:
    """Stress tests EphemeralBridge HTTP /v1/chat/completions endpoint under adversarial conditions."""

    def test_http_mixed_turn_non_streaming(self, mock_ls, ephemeral_bridge):
        """Verify non-streaming delivery of mixed text and tool calls via HTTP."""
        mock_ls.set_mode(
            "custom",
            response={
                "response": (
                    "I will query the database now.\n"
                    '<tool_call>\n{"name": "query_db", "arguments": {"sql": "SELECT 1;"}}\n</tool_call>\n'
                    "Waiting for database response."
                )
            },
        )
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "Check database status"}],
            stream=False,
        )

        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            assert resp.status_code == 200
            data = resp.json()

        choice = data["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert "I will query the database now." in choice["message"]["content"]
        assert "Waiting for database response." in choice["message"]["content"]
        tcs = choice["message"]["tool_calls"]
        assert len(tcs) == 1
        assert tcs[0]["function"]["name"] == "query_db"
        assert json.loads(tcs[0]["function"]["arguments"]) == {"sql": "SELECT 1;"}

    def test_http_mixed_turn_streaming(self, mock_ls, ephemeral_bridge):
        """Verify streaming SSE delivery of mixed text and tool calls via HTTP."""
        mock_ls.set_mode(
            "custom",
            response={
                "response": (
                    "Analyzing inputs.\n"
                    '<tool_call>\n{"name": "ActionA", "arguments": {"x": 10}}\n</tool_call>\n'
                    '<tool_call>\n{"name": "ActionB", "arguments": {"y": 20}}\n</tool_call>\n'
                    "All actions dispatched."
                )
            },
        )
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "Run parallel actions"}],
            stream=True,
        )

        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            assert resp.status_code == 200
            events = parse_sse_events(resp.text)

        content_deltas = []
        tc_deltas = []
        finish_reasons = []

        for ev in events:
            choices = ev.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            if "content" in delta and delta["content"]:
                content_deltas.append(delta["content"])
            if "tool_calls" in delta and delta["tool_calls"]:
                tc_deltas.extend(delta["tool_calls"])
            if choices[0].get("finish_reason"):
                finish_reasons.append(choices[0]["finish_reason"])

        # Content must contain remaining text
        assert any("Analyzing inputs." in c for c in content_deltas)
        # Exactly 2 segregated tool calls
        assert len(tc_deltas) == 2
        assert tc_deltas[0]["function"]["name"] == "ActionA"
        assert tc_deltas[1]["function"]["name"] == "ActionB"
        assert tc_deltas[0]["index"] == 0
        assert tc_deltas[1]["index"] == 1
        # Finish reason must be tool_calls
        assert "tool_calls" in finish_reasons

    def test_http_malformed_json_salvage_in_streaming(self, mock_ls, ephemeral_bridge):
        """Verify streaming mode salvages tool call with trailing comma in arguments."""
        mock_ls.set_mode(
            "custom",
            response={
                "response": '<tool_call>\n{"name": "salvage_tool", "arguments": {"key": "val",}}\n</tool_call>'
            },
        )
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "salvage test"}],
            stream=True,
        )

        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            assert resp.status_code == 200
            events = parse_sse_events(resp.text)

        tc_deltas = [
            tc for ev in events for choice in ev.get("choices", [])
            for tc in choice.get("delta", {}).get("tool_calls", [])
        ]
        assert len(tc_deltas) == 1
        assert tc_deltas[0]["function"]["name"] == "salvage_tool"


# ===================================================================== #
#  9. Empirical Vulnerabilities & Architectural Edge Cases               #
# ===================================================================== #

class TestEmpiricalVulnerabilitiesAndCornerCases:
    """Hardened tests verifying elimination of confirmed failure modes and parser vulnerabilities."""

    def test_vulnerability_greedy_regex_argument_corruption(self):
        """Hardened: Fallback balanced brace parsing extracts arguments without capturing outer closing brace."""
        raw = '<tool_call>\n{"name": "test", "arguments": {"a": 1, "b": 2,}}\n</tool_call>'
        _, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 1
        raw_args = tcs[0]["function"]["arguments"]
        assert not raw_args.endswith("}}"), f"Corrupted with double closing brace: {raw_args}"
        assert raw_args.endswith("}"), f"Expected valid closing brace: {raw_args}"
        assert '"a": 1' in raw_args

    def test_vulnerability_unescaped_stringified_json_dropped_to_empty(self):
        """Hardened: Stringified JSON arguments with outer quotes are preserved and extracted."""
        raw = '<tool_call>\n{"name": "Write", "arguments": "{"file_path": "/tmp/file.txt"}"}\n</tool_call>'
        _, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 1
        raw_args = tcs[0]["function"]["arguments"]
        assert raw_args != "{}", f"Arguments incorrectly dropped to empty dict: {raw_args}"
        assert "/tmp/file.txt" in raw_args

    def test_vulnerability_array_of_tool_calls_in_tag_ignored(self):
        """Hardened: JSON array of tool calls in a single <tool_call> extracts all tool calls."""
        raw = (
            "<tool_call>\n"
            '[\n  {"name": "ToolA", "arguments": {"x": 1}},\n  {"name": "ToolB", "arguments": {"y": 2}}\n]\n'
            "</tool_call>"
        )
        remaining, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 2
        assert tcs[0]["function"]["name"] == "ToolA"
        assert tcs[1]["function"]["name"] == "ToolB"
        assert json.loads(tcs[0]["function"]["arguments"]) == {"x": 1}
        assert json.loads(tcs[1]["function"]["arguments"]) == {"y": 2}
        assert "<tool_call>" not in remaining

    def test_vulnerability_multiple_objects_in_tag_swallows_second(self):
        """Hardened: Multiple concatenated JSON objects inside one <tool_call> extracts all tool calls."""
        raw = (
            "<tool_call>\n"
            '{"name": "ToolA", "arguments": {"x": 1}}\n'
            '{"name": "ToolB", "arguments": {"y": 2}}\n'
            "</tool_call>"
        )
        _, tcs = bridge.parse_model_response(raw)
        assert len(tcs) == 2
        assert tcs[0]["function"]["name"] == "ToolA"
        assert tcs[1]["function"]["name"] == "ToolB"
        assert json.loads(tcs[0]["function"]["arguments"]) == {"x": 1}
        assert json.loads(tcs[1]["function"]["arguments"]) == {"y": 2}

    def test_vulnerability_non_string_raw_response_type_error(self):
        """Hardened: Non-string truthy raw_response is safely coerced without TypeError."""
        rem, tcs = bridge.parse_model_response(12345)  # type: ignore[arg-type]
        assert rem == "12345"
        assert tcs == []

    def test_vulnerability_non_string_role_attribute_error(self):
        """Hardened: Non-string role in messages is safely coerced to string without AttributeError."""
        prompt = bridge.messages_to_prompt([{"role": 12345, "content": "hi"}])
        assert "hi" in prompt
        assert isinstance(prompt, str)
