"""Adversarial Stress Test Suite for litellmproxy-multiharness (Challenger 2).

Mission:
Adversarially stress-test error handling, circuit breaker failover, and prompt flattening:
1. Repeated upstream empty responses (asserting 502 Bad Gateway is consistently returned across streaming & non-streaming).
2. Socket timeouts and abrupt connection drops (asserting 504 and 502 status codes, client disconnect tolerance, and recovery).
3. Rapid sequential requests while circuit breaker is in STATE_FAILOVER vs STATE_PROBING (model routing, probe failure recovery, probe success transition, concurrency thread safety, error discrimination).
4. Multi-turn conversations ending with multiple layered system hooks (UserPromptSubmit, Plan mode, custom hooks, TOOL RESULT endings, empty user turns, deduplication of Caveman reminders, buddy comment stripping).
5. Historical session 7f8bfd5b replay with injected latency and synthetic error drops (verifying failover to remote fallback and zero synthetic recovery loops).
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List
from urllib import error

import httpx
import pytest

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
    STATE_FAILOVER,
    STATE_PRIMARY,
    STATE_PROBING,
    is_failover_error,
)


# ===================================================================== #
#  Fixtures                                                             #
# ===================================================================== #


@pytest.fixture
def mock_ls():
    """Isolated MockLanguageServer on dynamic port (127.0.0.1:0)."""
    server = MockLanguageServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def ephemeral_bridge(mock_ls):
    """Isolated EphemeralBridge wired to mock_ls on dynamic port (127.0.0.1:0)."""
    bridge_inst = EphemeralBridge(mock_ls)
    bridge_inst.start()
    try:
        yield bridge_inst
    finally:
        bridge_inst.stop()


@pytest.fixture
def mock_fallback():
    """Isolated MockRemoteFallbackServer on dynamic port (127.0.0.1:0)."""
    server = MockRemoteFallbackServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def session_fixture():
    """Session 7f8bfd5b turns fixture."""
    return load_session_fixture()


# ===================================================================== #
#  1. Repeated Upstream Empty Responses Stress Tests                    #
# ===================================================================== #


class TestAdversarialEmptyResponses:
    """Stress testing repeated upstream empty responses and payload edge cases."""

    def test_stress_50_consecutive_empty_dict_responses(self, mock_ls, ephemeral_bridge):
        """Stress test: 50 consecutive non-streaming requests with upstream returning empty dict {}.

        Every single request must return HTTP 502 Bad Gateway with standard error structure.
        """
        mock_ls.set_mode("empty")
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "Adversarial stress test empty response"}],
            stream=False,
        )

        with ephemeral_bridge.get_client() as client:
            statuses = []
            for i in range(50):
                resp = client.post("/v1/chat/completions", json=payload)
                statuses.append(resp.status_code)
                assert resp.status_code == 502, f"Request {i} expected 502, got {resp.status_code}: {resp.text}"
                data = resp.json()
                assert data["error"]["code"] == 502
                assert data["error"]["type"] == "bad_gateway"
                assert "empty completion" in data["error"]["message"].lower()

            assert len(statuses) == 50
            assert set(statuses) == {502}

    def test_stress_50_consecutive_empty_streaming_responses(self, mock_ls, ephemeral_bridge):
        """Stress test: 50 consecutive streaming requests with upstream returning empty string.

        Must return HTTP 502 Bad Gateway before streaming starts (never empty 200 SSE stream).
        """
        mock_ls.set_mode("empty_string")
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "Adversarial stress test empty streaming"}],
            stream=True,
        )

        with ephemeral_bridge.get_client() as client:
            statuses = []
            for i in range(50):
                resp = client.post("/v1/chat/completions", json=payload)
                statuses.append(resp.status_code)
                assert resp.status_code == 502, (
                    f"Streaming request {i} expected 502, got {resp.status_code}: {resp.text}"
                )
                data = resp.json()
                assert data["error"]["code"] == 502
                assert data["error"]["type"] == "bad_gateway"

            assert len(statuses) == 50
            assert set(statuses) == {502}

    def test_stress_empty_payload_variants(self, mock_ls, ephemeral_bridge):
        """Adversarially test various empty upstream payload variants."""
        variants = [
            ("whitespace_newlines", {"response": "   \n\n\t\r  \n  "}),
            ("explicit_none_response", {"response": None}),
            ("missing_response_key", {"other_key": "some_value"}),
            ("empty_response_with_metadata", {"response": "", "model_metadata": {"tokens": 0}}),
        ]

        with ephemeral_bridge.get_client() as client:
            for variant_name, custom_data in variants:
                mock_ls.set_mode("custom", response=custom_data)
                payload = build_chat_payload(
                    messages=[{"role": "user", "content": f"Test variant {variant_name}"}],
                    stream=False,
                )
                resp = client.post("/v1/chat/completions", json=payload)
                assert resp.status_code == 502, (
                    f"Variant '{variant_name}' expected 502, got {resp.status_code}: {resp.text}"
                )
                err = resp.json().get("error", {})
                assert err.get("code") == 502
                assert err.get("type") == "bad_gateway"

    def test_stress_empty_demarcations_treated_as_empty(self, mock_ls, ephemeral_bridge):
        """Upstream returning empty or malformed demarcation with no tool name and no text."""
        # When model returns demarcation with no tool name and no other text, it should not produce valid tool call
        mock_ls.set_mode("custom", response={"response": "<tool_call>{\"arguments\": {}}</tool_call>"})
        payload = build_chat_payload(
            messages=[{"role": "user", "content": "demarcation without name"}],
            stream=False,
        )
        with ephemeral_bridge.get_client() as client:
            resp = client.post("/v1/chat/completions", json=payload)
            # The parser treats it as raw text since name is missing
            assert resp.status_code == 200
            data = resp.json()
            # Must NOT be marked finish_reason: tool_calls
            assert data["choices"][0]["finish_reason"] == "stop"


# ===================================================================== #
#  2. Socket Timeouts and Abrupt Connection Drops Stress Tests          #
# ===================================================================== #


class TestAdversarialSocketTimeoutsAndConnectionDrops:
    """Stress testing socket timeouts, network drops, client disconnects, and recovery."""

    def test_stress_socket_timeout_mapping_504(self, mock_ls, ephemeral_bridge, monkeypatch):
        """Verify socket.timeout, urllib TimeoutError, and URLError(socket.timeout) map to 504."""
        payload = build_chat_payload(messages=[{"role": "user", "content": "test timeout"}])

        timeout_exceptions = [
            socket.timeout("The read operation timed out"),
            TimeoutError("Connection timed out"),
            error.URLError(socket.timeout("Read timed out")),
            error.URLError(TimeoutError("Operation timed out")),
        ]

        with ephemeral_bridge.get_client() as client:
            for exc in timeout_exceptions:
                def raise_timeout(*args, **kwargs):
                    raise exc

                monkeypatch.setattr(bridge, "rpc", raise_timeout)
                resp = client.post("/v1/chat/completions", json=payload)
                assert resp.status_code == 504, f"Expected 504 for {type(exc)}, got {resp.status_code}: {resp.text}"
                data = resp.json()
                assert data["error"]["code"] == 504
                assert data["error"]["type"] == "gateway_timeout"
                assert "timed out" in data["error"]["message"].lower()

    def test_stress_abrupt_network_connection_drops_502(self, mock_ls, ephemeral_bridge):
        """Verify live TCP RST / connection drop right after handshake returns HTTP 502."""
        mock_ls.set_mode("connection_drop")
        payload = build_chat_payload(messages=[{"role": "user", "content": "test drop"}])

        with ephemeral_bridge.get_client() as client:
            for i in range(10):
                resp = client.post("/v1/chat/completions", json=payload)
                assert resp.status_code == 502, f"Drop {i} expected 502, got {resp.status_code}: {resp.text}"
                data = resp.json()
                assert data["error"]["code"] == 502
                assert data["error"]["type"] == "bad_gateway"
                assert "failed" in data["error"]["message"].lower()

    def test_stress_recovery_after_consecutive_connection_drops(self, mock_ls, ephemeral_bridge):
        """Verify bridge survives 20 consecutive network drops and immediately recovers when upstream returns."""
        mock_ls.set_mode("connection_drop")
        payload = build_chat_payload(messages=[{"role": "user", "content": "recovery test"}])

        with ephemeral_bridge.get_client() as client:
            # 20 drops
            for i in range(20):
                resp = client.post("/v1/chat/completions", json=payload)
                assert resp.status_code == 502

            # Upstream recovers
            mock_ls.set_mode("normal")
            resp_healthy = client.post("/v1/chat/completions", json=payload)
            assert resp_healthy.status_code == 200, f"Expected 200 on recovery, got {resp_healthy.status_code}"
            assert resp_healthy.json()["choices"][0]["message"]["content"]

    def test_stress_client_abrupt_disconnect_during_streaming(self, mock_ls, ephemeral_bridge):
        """Verify bridge handles downstream client closing connection midway without crashing."""
        mock_ls.set_mode("normal")

        # Open raw socket connection, send streaming request, then immediately close
        s = socket.socket()
        s.connect((ephemeral_bridge.host, ephemeral_bridge.port))
        raw_body = json.dumps({
            "model": "antigravity-flash",
            "messages": [{"role": "user", "content": "stream disconnect test"}],
            "stream": True,
        }).encode("utf-8")
        req_headers = (
            f"POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(raw_body)}\r\n\r\n"
        ).encode("utf-8")
        s.sendall(req_headers + raw_body)
        # Abruptly close socket immediately before or during reading
        s.close()

        # Give bridge a few milliseconds to process the broken pipe
        time.sleep(0.05)

        # Bridge must remain fully operational for the next request
        with ephemeral_bridge.get_client() as client:
            resp = client.post(
                "/v1/chat/completions",
                json=build_chat_payload([{"role": "user", "content": "post disconnect alive test"}]),
            )
            assert resp.status_code == 200
            assert resp.json()["choices"][0]["message"]["content"]


# ===================================================================== #
#  3. Rapid Sequential Requests in STATE_FAILOVER vs STATE_PROBING      #
# ===================================================================== #


class TestAdversarialCircuitBreakerSequentialAndProbing:
    """Stress testing circuit breaker state transitions, probe behavior, and concurrency."""

    def test_stress_100_rapid_requests_in_state_failover(self):
        """Verify 100 rapid sequential requests in STATE_FAILOVER strictly route to remote fallback.

        Verify remote successes do NOT prematurely reset the circuit breaker back to primary.
        """
        current_time = 1000.0
        cb = AntigravityCircuitBreaker(cooldown_seconds=900.0, time_fn=lambda: current_time)

        # Trigger failover
        cb.record_failure("502 Bad Gateway: upstream empty")
        assert cb.state == STATE_FAILOVER

        models_to_test = [
            ("antigravity-flash", "antigravity-remote-flash"),
            ("antigravity-flash-high", "antigravity-remote-flash-high"),
            ("antigravity-flash-medium", "antigravity-remote-flash-medium"),
            ("antigravity-flash-low", "antigravity-remote-flash-low"),
            ("antigravity-pro", "antigravity-remote-pro"),
            ("antigravity-opus-4.6", "antigravity-remote-opus-4.6"),
        ]

        # Rapidly query targets across 100 iterations
        for i in range(100):
            # Advance time slightly within cooldown
            current_time += 1.0  # 1000 -> 1100, still well below 1900s
            assert cb.state == STATE_FAILOVER, f"Breaker prematurely left failover at iteration {i}"

            for req_model, expected_target in models_to_test:
                target = cb.get_target_for_model(req_model)
                assert target == expected_target, (
                    f"Iteration {i}: expected {expected_target}, got {target}"
                )

            # Record remote fallback success: must be ignored and not reset breaker
            res = cb.record_success("antigravity-remote-flash-medium")
            assert res is False, "Remote success must not reset circuit breaker"
            assert cb.state == STATE_FAILOVER

    def test_stress_state_probing_failure_immediately_reverts_to_failover(self):
        """Verify when in STATE_PROBING, a probe failure immediately reverts to STATE_FAILOVER.

        Also verifies that a fresh cooldown window is enforced from the time of the probe failure.
        """
        simulated_time = 1000.0
        cb = AntigravityCircuitBreaker(cooldown_seconds=900.0, time_fn=lambda: simulated_time)

        # 1. Initial failure at t=1000
        cb.record_failure("502 Bad Gateway")
        assert cb.state == STATE_FAILOVER

        # 2. Advance time past cooldown (t=1901)
        simulated_time = 1901.0
        assert cb.state == STATE_PROBING

        # In probing, target resolves to base model for probe attempt
        target = cb.get_target_for_model("antigravity-flash-high")
        assert target == "antigravity-flash-high"

        # 3. Probe request fails with 504 Gateway Timeout
        probe_recorded = cb.record_failure("504 Gateway Timeout")
        assert probe_recorded is True
        assert cb.state == STATE_FAILOVER

        # Target must immediately route back to remote model
        assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-remote-flash-high"

        # 4. Verify fresh cooldown from t=1901: at t=2500 (600s after failure), still in failover
        simulated_time = 2500.0
        assert cb.state == STATE_FAILOVER
        assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-remote-flash-high"

        # 5. At t=2802 (901s after second failure), transitions back to probing
        simulated_time = 2802.0
        assert cb.state == STATE_PROBING

    def test_stress_state_probing_success_restores_state_primary(self):
        """Verify when in STATE_PROBING, a successful probe restores STATE_PRIMARY."""
        simulated_time = 1000.0
        cb = AntigravityCircuitBreaker(cooldown_seconds=900.0, time_fn=lambda: simulated_time)

        cb.record_failure("502 Bad Gateway")
        simulated_time = 1905.0
        assert cb.state == STATE_PROBING

        # Success on base primary model
        res = cb.record_success("antigravity-flash-high")
        assert res is True, "Probe success on base model must restore circuit breaker"
        assert cb.state == STATE_PRIMARY

        # Target remains on base primary model
        assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-flash-high"

    def test_stress_concurrent_threads_circuit_breaker_integrity(self):
        """Stress test: 20 concurrent threads reading and writing circuit breaker with state file.

        Verifies thread safety, atomic file writing, and lack of file corruption or crashes.
        """
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "failover_state.json"
            cb = AntigravityCircuitBreaker(state_file=state_file)

            errors: List[Exception] = []

            def worker(tid: int):
                try:
                    for _ in range(50):
                        cb.record_failure("502 Bad Gateway")
                        _ = cb.state
                        target = cb.get_target_for_model("antigravity-flash")
                        assert "remote" in target
                        cb.record_success("antigravity-remote-flash-medium")
                        # State should remain failover despite remote success
                        assert cb.state == STATE_FAILOVER
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(errors) == 0, f"Encountered concurrency errors: {errors}"
            # Verify on-disk JSON is valid
            assert state_file.exists()
            data = json.loads(state_file.read_text(encoding="utf-8"))
            assert data.get("state") == STATE_FAILOVER

    def test_stress_failover_error_discrimination(self):
        """Verify circuit breaker discriminates between true failover errors vs non-failover errors."""
        class DummyError(Exception):
            def __init__(self, msg, code):
                super().__init__(msg)
                self.status_code = code

        failover_samples = [
            DummyError("Bad Gateway", 502),
            DummyError("Gateway Timeout", 504),
            DummyError("Too Many Requests", 429),
            "502 Bad Gateway",
            "504 Gateway Timeout",
            "Resource has been exhausted",
            "Your AI credits balance is too low to continue",
            {"status_code": 502, "message": "Bad Gateway"},
            {"code": 504, "type": "gateway_timeout"},
            {"error": {"status": "resource_exhausted"}},
        ]

        non_failover_samples = [
            None,
            "",
            SyntaxError("unexpected EOF"),
            TypeError("NoneType object is not callable"),
            KeyError("model"),
            ValueError("invalid literal for int()"),
            {"status_code": 400, "message": "Bad Request: invalid schema"},
            {"status_code": 401, "message": "Unauthorized"},
            {"status_code": 404, "message": "Not Found"},
            "Token limit 4096 exceeded in prompt context window",
            "Port 5596 is already in use",
        ]

        for s in failover_samples:
            assert is_failover_error(s) is True, f"Expected failover error for: {s}"

        for s in non_failover_samples:
            assert is_failover_error(s) is False, f"Expected NON-failover error for: {s}"

        # Note: Bare integer status codes without exception or dict wrapper return False
        # by design to prevent false positive regex matches on token counts / port numbers.
        assert is_failover_error(502) is False
        assert is_failover_error(504) is False


# ===================================================================== #
#  4. Multi-Turn Conversations Ending with Layered System Hooks         #
# ===================================================================== #


class TestAdversarialPromptNormalizationLayeredHooks:
    """Stress testing prompt flattening with multi-turn conversations and layered system hooks."""

    def test_stress_layered_system_hooks_precede_active_user_turn(self):
        """Verify multiple trailing system hooks (UserPromptSubmit, Plan mode, custom) precede user turn.

        Enforces that the assembled prompt ALWAYS terminates with explicit USER: invocation and ASSISTANT: cue.
        """
        messages = [
            {"role": "user", "content": "Turn 1: Initialize workspace setup"},
            {"role": "assistant", "content": "Turn 1: Workspace setup complete."},
            {"role": "user", "content": "Turn 2: Run verification tests for IAESTE-Hamburg e.V."},
            # Layered system hooks injected after user turn:
            {"role": "system", "content": "UserPromptSubmit hook additional context: CAVEMAN MODE ACTIVE (caveman)."},
            {"role": "system", "content": "Plan mode active: read-only mode, do not modify files without confirmation."},
            {"role": "system", "content": "Custom hook: Always add hints/follow-up questions for IAESTE-Hamburg e.V."},
            {"role": "developer", "content": "Developer instructions: enforce strict JSON schemas."},
        ]

        prompt = bridge.messages_to_prompt(messages)

        # 1. Prompt must terminate with explicit USER turn followed by ASSISTANT cue
        assert prompt.rstrip().endswith("ASSISTANT:")
        # The text before ASSISTANT: must be the user turn
        before_assistant = prompt.rstrip()[: -len("ASSISTANT:")].rstrip()
        assert before_assistant.endswith("USER: Turn 2: Run verification tests for IAESTE-Hamburg e.V.")

        # 2. All hooks must still be preserved in the prompt
        assert "CAVEMAN MODE ACTIVE" in prompt
        assert "Plan mode active" in prompt
        assert "IAESTE-Hamburg e.V." in prompt
        assert "Developer instructions" in prompt

        # 3. Hooks must precede the active user turn
        user_pos = prompt.rfind("USER: Turn 2: Run verification tests for IAESTE-Hamburg e.V.")
        caveman_pos = prompt.find("CAVEMAN MODE ACTIVE")
        plan_pos = prompt.find("Plan mode active")
        dev_pos = prompt.find("Developer instructions")

        assert caveman_pos < user_pos, "Caveman hook must appear before active user turn"
        assert plan_pos < user_pos, "Plan mode hook must appear before active user turn"
        assert dev_pos < user_pos, "Developer hook must appear before active user turn"

    def test_stress_conversation_ending_with_tool_result_and_hooks(self):
        """Verify conversations with tool execution turns followed by system hooks avoid trailing hooks."""
        messages = [
            {"role": "user", "content": "Read configuration file"},
            {
                "role": "assistant",
                "content": "Reading file...",
                "tool_calls": [
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{\"path\": \"config.yaml\"}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_abc123",
                "content": "port: 5596\nhost: 127.0.0.1",
            },
            {"role": "system", "content": "UserPromptSubmit hook: CAVEMAN MODE ACTIVE"},
            {"role": "system", "content": "Plan mode active"},
        ]

        prompt = bridge.messages_to_prompt(messages)

        # Trailing system hooks must be moved before user message
        assert prompt.rstrip().endswith("ASSISTANT:")
        assert "TOOL RESULT (read_file): port: 5596\nhost: 127.0.0.1" in prompt
        # The prompt must NOT end with a SYSTEM: block before ASSISTANT:
        parts = prompt.split("\n\n")
        assert not parts[-2].startswith("SYSTEM:"), f"Expected non-system block before ASSISTANT:, got: {parts[-2]}"

    def test_stress_conversation_with_no_user_turn(self):
        """Edge case: Conversation containing only system/developer messages and no user turn.

        Prompt normalizer must synthesize explicit USER: invocation before ASSISTANT cue.
        """
        messages = [
            {"role": "system", "content": "You are a code refactoring assistant."},
            {"role": "system", "content": "UserPromptSubmit hook: Plan mode enabled."},
        ]

        prompt = bridge.messages_to_prompt(messages)
        assert "USER: Please proceed with the above instructions." in prompt
        assert prompt.rstrip().endswith("ASSISTANT:")

        # Verify order: SYSTEM blocks -> synthetic USER -> ASSISTANT:
        sys_pos = prompt.rfind("SYSTEM:")
        user_pos = prompt.rfind("USER:")
        asst_pos = prompt.rfind("ASSISTANT:")
        assert sys_pos < user_pos < asst_pos

    def test_stress_whitespace_only_user_message_normalized(self):
        """Edge case: User message has only whitespace or comments that get stripped."""
        messages = [
            {"role": "user", "content": "   \n\t  <!-- buddy: status comment -->   "},
            {"role": "system", "content": "Plan mode hook"},
        ]

        prompt = bridge.messages_to_prompt(messages)
        # Stripped user message should trigger synthetic fallback user turn
        assert "USER: Please proceed with the above instructions." in prompt
        assert prompt.rstrip().endswith("ASSISTANT:")

    def test_stress_caveman_deduplication_across_10_turns(self):
        """Stress test: 10 conversational turns, each having CAVEMAN MODE reminders.

        Only the latest reminder must be retained, preventing context bloat.
        """
        messages = []
        for i in range(1, 11):
            messages.append({"role": "user", "content": f"User question {i}"})
            messages.append({"role": "assistant", "content": f"Assistant answer {i}"})
            messages.append({
                "role": "system",
                "content": f"<system-reminder>CAVEMAN MODE ACTIVE (turn {i}). Terse.</system-reminder>",
            })

        prompt = bridge.messages_to_prompt(messages)
        # Count occurrences of CAVEMAN MODE ACTIVE in prompt
        count = prompt.count("CAVEMAN MODE ACTIVE")
        assert count == 1, f"Expected exactly 1 Caveman reminder, found {count}"
        assert "turn 10" in prompt, "The retained reminder must be from the latest turn"

    def test_stress_buddy_comments_stripped_completely(self):
        """Verify statusline buddy comments (<!-- buddy: ... -->) are completely stripped."""
        messages = [
            {
                "role": "user",
                "content": "Perform task <!-- buddy: {\"status\": \"thinking\", \"time\": 12345} --> and verify",
            },
            {
                "role": "assistant",
                "content": "Starting task <!-- buddy: update --> now",
            },
        ]

        prompt = bridge.messages_to_prompt(messages)
        assert "<!-- buddy:" not in prompt
        assert "buddy:" not in prompt


# ===================================================================== #
#  5. Historical Session Replay with Latency & Synthetic Error Drops    #
# ===================================================================== #


class TestAdversarialHistoricalSessionReplayStress:
    """Stress testing session 7f8bfd5b replay with injected latency and synthetic error drops."""

    @pytest.mark.asyncio
    async def test_stress_session_7f8bfd5b_replay_with_latency_and_synthetic_drops(
        self, mock_ls, ephemeral_bridge, mock_fallback, session_fixture
    ):
        """Replay all 8 human user turns from session 7f8bfd5b with:

        - Upstream latency injection (0.05s).
        - Synthetic 502 error drops injected on turns 2 and 4.
        - Verification that StripOrphanToolChoiceGuard catches 502 and routes to remote fallback.
        - Verification that remote fallback responds with valid tokens.
        - Verification that 0 turns complete empty and synthetic recovery loop is NEVER triggered.
        """
        human_turns = [t for t in session_fixture["turns"] if not t["is_synthetic_recovery"]]
        assert len(human_turns) == 8
        tools = session_fixture["tools"]

        # Injected latency on primary mock LS
        mock_ls.delay = 0.05

        cb = AntigravityCircuitBreaker()
        guard = StripOrphanToolChoiceGuard(circuit_breaker=cb)

        turn_results = []

        async with httpx.AsyncClient(timeout=10.0) as client:
            for idx, turn in enumerate(human_turns):
                turn_idx = turn["turn_index"]
                user_input = turn["user_input"]
                system_hooks = turn.get("system_hooks", [])

                # Inject synthetic error drop on turns 2 and 4 (indices 2 and 4)
                is_synthetic_drop_turn = (idx in (2, 4))
                if is_synthetic_drop_turn:
                    mock_ls.set_mode("empty")
                elif "memory" in user_input.lower() or "memoery" in user_input.lower():
                    mock_ls.set_mode("tool_call")
                else:
                    mock_ls.set_mode("normal")

                messages = [{"role": "system", "content": h} for h in system_hooks]
                messages.append({"role": "user", "content": user_input})

                # 1. Pre-call hook resolves model via circuit breaker
                req_data = {
                    "model": "antigravity-flash",
                    "messages": messages,
                    "tools": tools,
                }
                routed_req = await guard.async_pre_call_hook({}, None, req_data, "chat")
                target_model = routed_req["model"]

                # 2. Dispatch request
                if "remote" in target_model:
                    # Routed directly to remote fallback server
                    resp = await client.post(
                        f"{mock_fallback.base_url}/v1/chat/completions",
                        json=routed_req,
                    )
                    assert resp.status_code == 200
                    await guard.async_post_call_success_hook(routed_req, {}, resp.json())
                else:
                    # Routed to primary bridge
                    resp = await client.post(
                        f"{ephemeral_bridge.base_url}/v1/chat/completions",
                        json=routed_req,
                    )
                    if resp.status_code in (502, 504):
                        # Catch error in post-call failure hook
                        await guard.async_post_call_failure_hook(
                            routed_req, Exception(f"HTTP {resp.status_code}"), {}
                        )
                        assert cb.state == STATE_FAILOVER

                        # Auto-retry on fallback deployment
                        fallback_req = await guard.async_pre_call_hook(
                            {}, None, {"model": "antigravity-flash", "messages": messages, "tools": tools}, "chat"
                        )
                        assert "remote" in fallback_req["model"]
                        resp = await client.post(
                            f"{mock_fallback.base_url}/v1/chat/completions",
                            json=fallback_req,
                        )
                        assert resp.status_code == 200
                        await guard.async_post_call_success_hook(fallback_req, {}, resp.json())

                # 3. Assert output validity
                data = resp.json()
                choice = data["choices"][0]
                content = choice.get("message", {}).get("content") or ""
                tool_calls = choice.get("message", {}).get("tool_calls") or []

                has_output = bool(content.strip()) or len(tool_calls) > 0
                turn_results.append({
                    "turn_index": turn_idx,
                    "synthetic_drop": is_synthetic_drop_turn,
                    "has_output": has_output,
                    "content_length": len(content),
                    "tool_calls_count": len(tool_calls),
                    "cb_state": cb.state,
                })

        # Programmatic Verification Assertions:
        # 1. All 8 human turns must produce output
        assert len(turn_results) == 8
        for r in turn_results:
            assert r["has_output"] is True, f"Turn {r['turn_index']} produced 0 output!"

        # 2. Turns 2 and 4 successfully triggered failover
        assert cb.state == STATE_FAILOVER, f"Expected circuit breaker to be in failover, got {cb.state}"

        # 3. Memory tool call in historical failure turn 6 succeeded
        turn_6 = next(r for r in turn_results if r["turn_index"] == 6)
        assert turn_6["tool_calls_count"] == 1 or turn_6["content_length"] > 0
        assert turn_6["has_output"] is True

        # 4. Zero synthetic recovery loops
        empty_turns = [r for r in turn_results if not r["has_output"]]
        assert len(empty_turns) == 0, f"Found empty turns: {empty_turns}"
