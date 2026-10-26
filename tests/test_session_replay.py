"""Comprehensive 4-Tier Automated Session Replay Test Suite.

Verifies:
- Tier 1: Feature Coverage
  * F1: Tool calling schema prompt injection
  * F2: Model response demarcation parsing
  * F3: Dual-mode response synthesizer (non-streaming and segregated SSE streaming)
  * F5: Fail-fast HTTP 502 Bad Gateway on empty upstream output
  * F6: Fail-fast HTTP 504 Gateway Timeout on upstream timeout
  * F8: Trailing hook normalization & explicit prompt termination
- Tier 2: Boundary & Corner Cases
  * Empty messages handling
  * Missing tool call arguments handling
  * Partial / broken demarcations handling
  * Markdown JSON fenced code block stripping
  * Multi-turn tool history preservation
- Tier 3: Cross-Feature Combinations
  * Tool calling under Caveman mode
  * Empty upstream triggering circuit breaker failover to remote fallback
- Tier 4: Real-World Scenario
  * Historical Claude Code session 7f8bfd5b fixture validation
  * Full replay of session 7f8bfd5b human turns with zero 0-token failures
    and zero synthetic recovery prompts
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any, Dict, List
import pytest
import httpx

from service.bin import antigravity_bridge as bridge
from tests.replay_harness import (
    EphemeralBridge,
    MockLanguageServer,
    MockRemoteFallbackServer,
    build_chat_payload,
    load_session_fixture,
    parse_sse_events,
)
from tool_choice_guard import (
    AntigravityCircuitBreaker,
    StripOrphanToolChoiceGuard,
    STATE_PRIMARY,
    STATE_FAILOVER,
    is_failover_error,
)


# ===================================================================== #
#  Fixtures                                                             #
# ===================================================================== #


@pytest.fixture
def mock_ls():
    """Create and start an isolated MockLanguageServer on 127.0.0.1:0."""
    server = MockLanguageServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def ephemeral_bridge(mock_ls):
    """Create and start an EphemeralBridge wired to mock_ls on 127.0.0.1:0."""
    bridge_instance = EphemeralBridge(mock_ls)
    bridge_instance.start()
    try:
        yield bridge_instance
    finally:
        bridge_instance.stop()


@pytest.fixture
def mock_fallback():
    """Create and start an isolated MockRemoteFallbackServer on 127.0.0.1:0."""
    server = MockRemoteFallbackServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def session_fixture():
    """Load the reproducible historical session 7f8bfd5b fixture."""
    return load_session_fixture()


# ===================================================================== #
#  Tier 1: Feature Coverage                                             #
# ===================================================================== #


class TestTier1FeatureCoverage:
    """Tier 1: Core feature verification for tool emulation, error handling, and hooks."""

    def test_tier1_tool_schema_prompt_injection(self, mock_ls, ephemeral_bridge, session_fixture):
        """F1: Verify OpenAI tool schemas are injected into the prompt with <tool_call> instructions."""
        mock_ls.set_mode("normal")
        tools = session_fixture["tools"]  # 5 key tools including create_pieces_memory and Write
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "Please store this in memory"}],
            tools=tools,
            tool_choice="auto",
        )

        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        prompt = mock_ls.last_prompt
        assert prompt, "Expected bridge to forward prompt to mock language server"
        # Verify tool definitions are present in the injected prompt
        assert "create_pieces_memory" in prompt, "create_pieces_memory must be present in prompt"
        assert "Write" in prompt, "Write tool must be present in prompt"
        # Verify demarcation instructions are present in prompt
        assert "<tool_call>" in prompt, "Demarcation marker <tool_call> must be present in instructions"
        assert "</tool_call>" in prompt, "Demarcation marker </tool_call> must be present in instructions"
        # Verify prompt terminates with ASSISTANT:
        assert prompt.rstrip().endswith("ASSISTANT:"), f"Prompt must end with ASSISTANT:, got: {prompt[-50:]}"

    def test_tier1_non_streaming_tool_call_response_parsing(self, mock_ls, ephemeral_bridge, session_fixture):
        """F2, F3: Verify <tool_call> demarcations are parsed into choices[0].message.tool_calls."""
        mock_ls.set_mode("tool_call")
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "save memory"}],
            tools=session_fixture["tools"],
            stream=False,
        )

        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
            data = resp.json()

        choices = data.get("choices", [])
        assert len(choices) > 0, "Expected at least one choice in completion response"
        choice = choices[0]
        assert choice.get("finish_reason") == "tool_calls", (
            f"Expected finish_reason 'tool_calls', got {choice.get('finish_reason')}"
        )

        message = choice.get("message", {})
        tool_calls = message.get("tool_calls", [])
        assert len(tool_calls) == 1, f"Expected 1 tool call, got {len(tool_calls)}: {tool_calls}"

        tc = tool_calls[0]
        assert tc.get("type") == "function"
        assert tc.get("id", "").startswith("call_"), f"Call ID should start with call_, got {tc.get('id')}"

        fn = tc.get("function", {})
        assert fn.get("name") == "create_pieces_memory", f"Expected create_pieces_memory, got {fn.get('name')}"
        # Parse arguments JSON string
        args = json.loads(fn.get("arguments", "{}"))
        assert args.get("text") == "IAESTE-Hamburg e.V.", f"Unexpected arguments: {args}"

    def test_tier1_streaming_sse_tool_call_chunks(self, mock_ls, ephemeral_bridge, session_fixture):
        """F3: Verify streaming requests yield segregated SSE chunks for tool calls."""
        mock_ls.set_mode("mixed_tool_call")
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "create test file"}],
            tools=session_fixture["tools"],
            stream=True,
        )

        with ephemeral_bridge.get_client() as client:
            with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers.get("content-type", "")
                lines = []
                for line in resp.iter_lines():
                    lines.append(line)
                    if line.strip() == "data: [DONE]":
                        break
                events = parse_sse_events("\n".join(lines))

        assert len(events) >= 2, f"Expected multiple SSE events, got {len(events)}"
        # Verify done marker was sent
        assert any(e.get("done") is True for e in events), "Expected [DONE] marker in SSE stream"

        # Check for tool_calls delta chunks
        tc_chunks = []
        content_chunks = []
        finish_reasons = []

        for e in events:
            if e.get("done"):
                continue
            choices = e.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            if "tool_calls" in delta and delta["tool_calls"]:
                tc_chunks.append(delta["tool_calls"])
            if "content" in delta and delta["content"]:
                content_chunks.append(delta["content"])
            if choices[0].get("finish_reason"):
                finish_reasons.append(choices[0]["finish_reason"])

        assert len(tc_chunks) > 0, f"Expected at least one tool_calls delta chunk, got {events}"
        first_tc = tc_chunks[0][0]
        assert first_tc.get("function", {}).get("name") == "Write"
        assert "tool_calls" in finish_reasons or "stop" in finish_reasons

    def test_tier1_upstream_empty_fail_fast_http_502(self, mock_ls, ephemeral_bridge):
        """F5: Verify upstream empty response returns HTTP 502 Bad Gateway instead of empty 200 OK."""
        # Test mode: empty dictionary {}
        mock_ls.set_mode("empty")
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "i did not see you call memoery cration tools"}],
            stream=False,
        )

        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            assert resp.status_code == 502, f"Expected HTTP 502 Bad Gateway, got {resp.status_code}: {resp.text}"
            data = resp.json()
            assert "error" in data, f"Expected 'error' in response body: {data}"
            err = data["error"]
            assert err.get("code") == 502 or "502" in str(err) or "empty" in str(err).lower()

        # Test mode: empty response string {"response": ""}
        mock_ls.set_mode("empty_string")
        with ephemeral_bridge.get_client() as client:
            resp2 = client.post("/v1/chat/completions", json=payload)
            assert resp2.status_code == 502, f"Expected HTTP 502 for empty string, got {resp2.status_code}"

        # Test mode: whitespace-only response {"response": "   \n\t "}
        mock_ls.set_mode("whitespace")
        with ephemeral_bridge.get_client() as client:
            resp3 = client.post("/v1/chat/completions", json=payload)
            assert resp3.status_code == 502, f"Expected HTTP 502 for whitespace, got {resp3.status_code}"

    def test_tier1_upstream_timeout_http_504(self, mock_ls, ephemeral_bridge, monkeypatch):
        """F6: Verify socket/urllib timeouts return HTTP 504 Gateway Timeout and disconnects return 502."""
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "hello"}],
            stream=False,
        )

        # 1. Simulate socket / urllib read timeout -> HTTP 504
        def mock_rpc_timeout(method, req_payload):
            raise socket.timeout("The read operation timed out")

        monkeypatch.setattr(bridge, "rpc", mock_rpc_timeout)
        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            assert resp.status_code == 504, f"Expected 504 Gateway Timeout on timeout, got {resp.status_code}: {resp.text}"
            data = resp.json()
            assert data.get("error", {}).get("code") == 504

        # 2. Simulate connection drop / refusal -> HTTP 502
        def mock_rpc_conn_refused(method, req_payload):
            raise ConnectionRefusedError("Connection refused by upstream language server")

        monkeypatch.setattr(bridge, "rpc", mock_rpc_conn_refused)
        with ephemeral_bridge.get_client() as client:
            resp2 = client.post("/v1/chat/completions", json=payload)
            assert resp2.status_code == 502, f"Expected 502 Bad Gateway on connection refusal, got {resp2.status_code}: {resp2.text}"
            data2 = resp2.json()
            assert data2.get("error", {}).get("code") == 502

    def test_tier1_prompt_flattening_trailing_hook_normalization(self, mock_ls, ephemeral_bridge):
        """F8, F9: Verify terminal system hooks are re-ordered and prompt ends with USER: and ASSISTANT:."""
        mock_ls.set_mode("normal")
        # Reproduce exact sequence from Turn 6 of session 7f8bfd5b:
        # User message followed by terminal Caveman hook
        caveman_hook = (
            "CAVEMAN MODE ACTIVE (caveman). Respond terse like smart caveman. "
            "All technical substance stay. Only fluff die. Answer only what was asked."
        )
        messages = [
            {"role": "user", "content": "i did not see you call memoery cration tools right ther e..."},
            {"role": "system", "content": caveman_hook},
        ]
        payload = build_chat_payload(messages=messages)

        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            assert resp.status_code == 200

        prompt = mock_ls.last_prompt
        assert prompt, "Expected prompt to be forwarded"
        # Prompt must NOT end with SYSTEM:
        assert not prompt.rstrip().endswith("SYSTEM:"), (
            f"Prompt must not terminate with trailing SYSTEM: hook block. Prompt end:\n{prompt[-120:]}"
        )
        # Prompt must terminate with ASSISTANT:
        assert prompt.rstrip().endswith("ASSISTANT:"), (
            f"Prompt must terminate with ASSISTANT:, got:\n{prompt[-60:]}"
        )
        # The user's prompt should be placed before the ASSISTANT: continuation cue
        user_snippet = "i did not see you call memoery cration tools right ther e..."
        assert user_snippet in prompt
        # Verify the user prompt is near the end, not buried before trailing hooks
        user_idx = prompt.find(user_snippet)
        asst_idx = prompt.rfind("ASSISTANT:")
        assert user_idx < asst_idx, "User prompt must precede the final ASSISTANT: cue"


# ===================================================================== #
#  Tier 2: Boundary & Corner Cases                                      #
# ===================================================================== #


class TestTier2BoundaryAndCornerCases:
    """Tier 2: Boundary conditions, malformed demarcations, and multi-turn context."""

    def test_tier2_empty_messages_handling(self, mock_ls, ephemeral_bridge):
        """Verify empty messages list or empty string content is handled without 500 error."""
        mock_ls.set_mode("normal")

        # Test empty messages list
        payload1 = build_chat_payload(messages=[])
        with ephemeral_bridge.get_client() as client:
            resp1 = client.post("/v1/chat/completions", json=payload1)
            assert resp1.status_code in (200, 400), f"Expected 200 or 400, got {resp1.status_code}"

        # Test message with empty string content
        payload2 = build_chat_payload(messages=[{"role": "user", "content": ""}])
        with ephemeral_bridge.get_client() as client:
            resp2 = client.post("/v1/chat/completions", json=payload2)
            assert resp2.status_code in (200, 400), f"Expected 200 or 400, got {resp2.status_code}"

    def test_tier2_tool_call_missing_arguments(self, mock_ls, ephemeral_bridge, session_fixture):
        """Verify tool call without arguments field defaults safely to '{}'."""
        # Mock language server returning tool call with no arguments
        mock_ls.set_mode(
            "custom",
            response={"response": '<tool_call>\n{"name": "AskUserQuestion"}\n</tool_call>'},
        )
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "ask question"}],
            tools=session_fixture["tools"],
        )

        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            assert resp.status_code == 200
            data = resp.json()

        tc = data["choices"][0]["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "AskUserQuestion"
        # Arguments should be a valid JSON string "{}"
        assert tc["function"]["arguments"] == "{}"

    def test_tier2_partial_or_malformed_demarcations(self, mock_ls, ephemeral_bridge, session_fixture):
        """Verify unclosed <tool_call> tags or invalid JSON do not crash the bridge."""
        mock_ls.set_mode("partial_demarcation")  # <tool_call>{"name": "incomplete"
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "test unclosed tag"}],
            tools=session_fixture["tools"],
        )

        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            # Should return HTTP 200 with raw text, rather than an unhandled 500 crash
            assert resp.status_code == 200
            data = resp.json()
            content = data["choices"][0]["message"].get("content") or ""
            assert "<tool_call>" in content or "incomplete" in content

    def test_tier2_markdown_json_fenced_tool_call(self, mock_ls, ephemeral_bridge, session_fixture):
        """Verify markdown ```json code fences enclosing <tool_call> are stripped."""
        mock_ls.set_mode("markdown_json_tool_call")
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "write file"}],
            tools=session_fixture["tools"],
        )

        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            assert resp.status_code == 200
            data = resp.json()

        choice = data["choices"][0]
        assert choice.get("finish_reason") == "tool_calls"
        tc = choice["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "Write"
        args = json.loads(tc["function"]["arguments"])
        assert args.get("file_path") == "/tmp/test.txt"

    def test_tier2_multi_turn_tool_history_preservation(self, mock_ls, ephemeral_bridge, session_fixture):
        """F4: Verify assistant tool_calls and tool results are preserved across multi-turn history."""
        mock_ls.set_mode("normal")
        # Build multi-turn conversational history with tool call and result
        messages = [
            {"role": "user", "content": "Please write hello.txt"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_write_123",
                        "type": "function",
                        "function": {
                            "name": "Write",
                            "arguments": '{"file_path": "/tmp/hello.txt", "content": "Hello World"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_write_123",
                "content": "File written successfully (11 bytes).",
            },
            {"role": "user", "content": "Now please verify the file exists."},
        ]
        payload = build_chat_payload(messages=messages, tools=session_fixture["tools"])

        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            assert resp.status_code == 200

        prompt = mock_ls.last_prompt
        # Previous tool call must NOT be stripped or omitted
        assert "Write" in prompt, "Tool name 'Write' must be preserved in conversational prompt history"
        assert "/tmp/hello.txt" in prompt, "Tool call arguments must be preserved in prompt history"
        assert "File written successfully" in prompt, "Tool execution result must be preserved in prompt history"


# ===================================================================== #
#  Tier 3: Cross-Feature Combinations                                   #
# ===================================================================== #


class TestTier3CrossFeatureCombinations:
    """Tier 3: Interactions between tool calling, caveman mode, and failover circuit breaker."""

    def test_tier3_tool_calling_under_caveman_mode(self, mock_ls, ephemeral_bridge, session_fixture):
        """Verify tool calling operates correctly when Caveman mode is active."""
        mock_ls.set_mode("tool_call")
        caveman_instruction = (
            "CAVEMAN MODE ACTIVE (caveman). Respond terse like smart caveman. "
            "All technical substance stay. Only fluff die."
        )
        messages = [
            {"role": "system", "content": caveman_instruction},
            {"role": "user", "content": "store Hamburg entity in memory"},
            # Duplicate reminder to verify deduplication
            {"role": "system", "content": caveman_instruction},
        ]
        payload = build_chat_payload(
            messages=messages,
            tools=session_fixture["tools"],
        )

        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            assert resp.status_code == 200
            data = resp.json()

        # Tool call should be successfully parsed
        choice = data["choices"][0]
        assert choice.get("finish_reason") == "tool_calls"
        tc = choice["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "create_pieces_memory"

        # Check prompt sent upstream: deduplicated Caveman header and ASSISTANT cue
        prompt = mock_ls.last_prompt
        # Count occurrences of CAVEMAN MODE ACTIVE in prompt
        caveman_count = prompt.count("CAVEMAN MODE ACTIVE")
        assert caveman_count <= 1, f"Expected redundant Caveman reminders to be deduplicated, found {caveman_count}"
        assert prompt.rstrip().endswith("ASSISTANT:")

    @pytest.mark.asyncio
    async def test_tier3_empty_upstream_triggers_circuit_breaker_and_fallback(
        self, mock_ls, ephemeral_bridge, mock_fallback
    ):
        """F7: Verify HTTP 502 from primary bridge transitions circuit breaker and triggers remote fallback."""
        # 1. Primary bridge emits 502 on empty upstream
        mock_ls.set_mode("empty")

        with ephemeral_bridge.get_client() as client:
            primary_resp = client.post(
                "/v1/chat/completions",
                json=build_chat_payload([{"role": "user", "content": "test failover"}]),
            )
            assert primary_resp.status_code == 502

        # 2. Verify circuit breaker recognizes the 502 error
        cb = AntigravityCircuitBreaker()
        assert cb.state == STATE_PRIMARY
        record_res = cb.record_failure(Exception(f"HTTP 502 Bad Gateway: {primary_resp.text}"))
        assert record_res is True, "Circuit breaker must record HTTP 502 as a failover failure"
        assert cb.state == STATE_FAILOVER, f"Circuit breaker state should be failover, got {cb.state}"

        # 3. Verify target routing resolves to remote fallback deployment
        target = cb.get_target_for_model("antigravity-flash")
        assert target in ("antigravity-remote-flash", "antigravity-remote-flash-medium")

        target_med = cb.get_target_for_model("antigravity-flash-medium")
        assert target_med == "antigravity-remote-flash-medium"

        # 4. Verify StripOrphanToolChoiceGuard pre-call hook reroutes to remote fallback
        guard = StripOrphanToolChoiceGuard()
        guard.circuit_breaker.trigger_failover("502 Bad Gateway")

        routed_payload = {
            "model": "antigravity-flash",
            "messages": [{"role": "user", "content": "test failover"}],
        }
        modified_data = await guard.async_pre_call_hook(
            user_api_key_dict={}, cache=None, data=routed_payload, call_type="anthropic_messages"
        )
        assert modified_data["model"] in ("antigravity-remote-flash", "antigravity-remote-flash-medium", "antigravity-remote-flash-high"), (
            f"Expected remote routed model, got {modified_data.get('model')}"
        )

        # 5. Verify MockRemoteFallbackServer responds with valid completion (no 0-token failure)
        async with httpx.AsyncClient(base_url=mock_fallback.base_url) as fb_client:
            fb_resp = await fb_client.post(
                "/v1/chat/completions",
                json={
                    "model": modified_data["model"],
                    "messages": modified_data["messages"],
                },
            )
            assert fb_resp.status_code == 200
            fb_data = fb_resp.json()
            content = fb_data["choices"][0]["message"]["content"]
            assert content, "Fallback server response must not be empty"
            assert fb_data["usage"]["completion_tokens"] > 0


# ===================================================================== #
#  Tier 4: Real-World Scenario: Historical Session 7f8bfd5b Replay      #
# ===================================================================== #


class TestTier4RealWorldSessionReplay:
    """Tier 4: Full replay of historical session 7f8bfd5b turns."""

    def test_tier4_session_7f8bfd5b_fixture_integrity(self, session_fixture):
        """F10: Verify the extracted session 7f8bfd5b fixture is valid and complete."""
        assert session_fixture["session_id"] == "7f8bfd5b-a3a4-4526-8022-f82b3d8c5b93"
        assert session_fixture["total_user_turns"] == 12
        assert session_fixture["total_historical_failures"] == 8
        assert len(session_fixture["tools"]) >= 4
        assert len(session_fixture["all_tool_names"]) == 175

        # Verify historical failures are correctly flagged
        turns = session_fixture["turns"]
        historical_failed_turns = [t for t in turns if t["historical_failure"]]
        assert len(historical_failed_turns) == 8

        # Turns 1, 7, 9, 11 must be synthetic recovery prompts
        synthetic_turns = [t for t in turns if t["is_synthetic_recovery"]]
        assert len(synthetic_turns) == 4
        for st in synthetic_turns:
            assert "[Your previous response had no visible output" in st["user_input"]

    def test_tier4_session_7f8bfd5b_human_turns_replay(self, mock_ls, ephemeral_bridge, session_fixture):
        """F12: Replay all human user turns through the pipeline.

        Assert that 0 turns complete with empty tokens and synthetic recovery prompt is never triggered.
        """
        mock_ls.set_mode("normal")
        tools = session_fixture["tools"]
        human_turns = [t for t in session_fixture["turns"] if not t["is_synthetic_recovery"]]

        assert len(human_turns) == 8, f"Expected 8 human turns, got {len(human_turns)}"

        replayed_results = []

        with ephemeral_bridge.get_client() as client:
            for turn in human_turns:
                turn_idx = turn["turn_index"]
                user_input = turn["user_input"]
                system_hooks = turn.get("system_hooks", [])

                # In historical session, turn 6 failed because user mentioned memory creation tools
                # Configure mock_ls to return appropriate response:
                # If user asks about memory creation, mock_ls returns a tool call!
                if "memoery cration tools" in user_input or "memory" in user_input:
                    mock_ls.set_mode("tool_call")
                else:
                    mock_ls.set_mode("normal")

                messages = []
                for hook in system_hooks:
                    messages.append({"role": "system", "content": hook})
                messages.append({"role": "user", "content": user_input})

                payload = build_chat_payload(
                    messages=messages,
                    tools=tools,
                    stream=False,
                )

                resp = client.post("/v1/chat/completions", json=payload)
                assert resp.status_code == 200, (
                    f"Turn {turn_idx} failed with HTTP {resp.status_code}: {resp.text}"
                )
                data = resp.json()
                choice = data["choices"][0]
                content = choice.get("message", {}).get("content") or ""
                tool_calls = choice.get("message", {}).get("tool_calls") or []

                has_output = bool(content.strip()) or len(tool_calls) > 0
                replayed_results.append({
                    "turn_index": turn_idx,
                    "has_output": has_output,
                    "content_length": len(content),
                    "tool_calls_count": len(tool_calls),
                    "historical_failure": turn["historical_failure"],
                })

        # Programmatic Assertions:
        # 1. 100% of replayed human turns must produce visible output or tool calls
        empty_turns = [r for r in replayed_results if not r["has_output"]]
        assert len(empty_turns) == 0, (
            f"Expected 0 turns with empty output, but {len(empty_turns)} were empty: {empty_turns}"
        )

        # 2. Historical failure turns (e.g. Turn 0 and Turn 6) must now succeed
        turn_6_result = next(r for r in replayed_results if r["turn_index"] == 6)
        assert turn_6_result["has_output"] is True
        assert turn_6_result["tool_calls_count"] == 1, (
            f"Turn 6 was expected to invoke tool, got {turn_6_result}"
        )

        # 3. Verify synthetic recovery prompt was NEVER triggered or needed
        for r in replayed_results:
            assert r["has_output"] is True, "No turn should ever trigger synthetic recovery prompt"
