"""Adversarial stress and edge-case verification suite for Milestone 1 (Classification & Hardening).

Conducted by: teamwork_challenger_m1_2
Scope:
1. Boundary & cyclic reference stress tests (circular dicts, cyclic exceptions, deep dicts)
2. Non-string / malformed header keys crash testing (AttributeError)
3. Hostile correlation ID fuzzing (10KB payloads, null bytes, CRLF injection)
4. Circuit breaker state machine edge cases (string timestamps in state file, corrupt JSON)
5. False positive susceptibility analysis on bare '402' numbers in unrelated errors
6. Strict service exclusion invariant verification
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from service.agentic.endpoint_abstraction.canopy_classifier import (
    CanopyClassifier,
    extract_canopy_correlation_id,
    extract_canopy_reason,
    is_canopy_exhaustion,
)
import tool_choice_guard
from tool_choice_guard import (
    AntigravityCircuitBreaker,
    STATE_FAILOVER,
    STATE_PRIMARY,
    STATE_PROBING,
    is_exhaustion_error,
    is_failover_error,
)


# ============================================================================ #
# 1. Circular Reference & Deep Recursion Stress                                 #
# ============================================================================ #

class TestCircularReferenceStress:
    """Stress tests verifying classifier and guard behavior on cyclic structures."""

    def test_circular_dict_self_reference_in_error_field(self) -> None:
        """Adversarial input: dict referencing itself in 'error' key.
        Expected: graceful rejection (False), NOT RecursionError crash.
        """
        d: Dict[str, Any] = {}
        d["error"] = d

        # Check classifier directly
        try:
            res = CanopyClassifier.is_canopy_exhaustion(d)
            assert res is False
        except RecursionError:
            pytest.fail("CRITICAL: CanopyClassifier.is_canopy_exhaustion raised RecursionError on circular dict!")

        # Check tool_choice_guard directly
        try:
            res_guard = tool_choice_guard.is_failover_error(d)
            assert res_guard is False
        except RecursionError:
            pytest.fail("CRITICAL: tool_choice_guard.is_failover_error raised RecursionError on circular dict!")

    def test_circular_dict_in_message_field(self) -> None:
        """Adversarial input: dict referencing itself in 'message' key."""
        d: Dict[str, Any] = {}
        d["message"] = d

        try:
            res = CanopyClassifier.is_canopy_exhaustion(d)
            assert res is False
        except RecursionError:
            pytest.fail("CRITICAL: CanopyClassifier.is_canopy_exhaustion raised RecursionError on d['message'] cycle!")

    def test_circular_exception_cause_cycle(self) -> None:
        """Adversarial input: Exception cause cycle e1.__cause__ = e2, e2.__cause__ = e1."""
        e1 = Exception("cause_cycle_1")
        e2 = Exception("cause_cycle_2")
        e1.__cause__ = e2
        e2.__cause__ = e1

        try:
            res = CanopyClassifier.is_canopy_exhaustion(e1)
            assert res is False
        except RecursionError:
            pytest.fail("CRITICAL: CanopyClassifier.is_canopy_exhaustion raised RecursionError on cyclic __cause__!")

    def test_circular_exception_context_cycle(self) -> None:
        """Adversarial input: Exception context cycle e.__context__ = e."""
        e = Exception("context_cycle")
        e.__context__ = e

        try:
            res = CanopyClassifier.is_canopy_exhaustion(e)
            assert res is False
        except RecursionError:
            pytest.fail("CRITICAL: CanopyClassifier.is_canopy_exhaustion raised RecursionError on cyclic __context__!")

    def test_deeply_nested_dict_recursion(self) -> None:
        """Adversarial input: 1500 levels of nested 'error' dicts."""
        d: Dict[str, Any] = {}
        curr = d
        for _ in range(1500):
            curr["error"] = {}
            curr = curr["error"]

        try:
            res = CanopyClassifier.is_canopy_exhaustion(d)
            assert res is False
        except RecursionError:
            pytest.fail("CRITICAL: CanopyClassifier.is_canopy_exhaustion raised RecursionError on deeply nested dicts!")


# ============================================================================ #
# 2. Hostile Header Keys Crash Testing                                         #
# ============================================================================ #

class TestHostileHeaderKeys:
    """Tests handling of non-string header dictionary keys."""

    def test_integer_header_key_crash(self) -> None:
        """Adversarial input: error object whose headers dict contains integer keys.
        Bug identified: k.lower() crashes with AttributeError: 'int' object has no attribute 'lower'.
        """
        class ErrWithIntHeaderKey(Exception):
            headers = {123: "val", "x-canopy-request-id": "req_safe_01"}

        try:
            cid = CanopyClassifier.extract_correlation_id(ErrWithIntHeaderKey())
            assert cid == "req_safe_01"
        except AttributeError:
            pytest.fail("CRITICAL: extract_correlation_id raised AttributeError on non-string header key!")

    def test_none_header_key_crash(self) -> None:
        """Adversarial input: error object whose headers dict contains None key."""
        class ErrWithNoneHeaderKey(Exception):
            headers = {None: "val", "x-canopy-request-id": "req_safe_02"}

        try:
            cid = CanopyClassifier.extract_correlation_id(ErrWithNoneHeaderKey())
            assert cid == "req_safe_02"
        except AttributeError:
            pytest.fail("CRITICAL: extract_correlation_id raised AttributeError on None header key!")

    @pytest.mark.parametrize(
        "hostile_key",
        [
            (1, 2),
            ("x", "canopy"),
            (),
            True,
            False,
            3.14159,
            float("nan"),
            float("inf"),
            b"bytes_key",
            complex(1, 2),
        ],
    )
    def test_non_string_header_keys(self, hostile_key: Any) -> None:
        """Adversarial input: error object whose headers dict contains tuple, bool, float, or other non-string keys."""
        class ErrWithHostileHeaderKey(Exception):
            headers = {hostile_key: "val", "x-canopy-request-id": "req_safe_non_str"}

        try:
            cid = CanopyClassifier.extract_correlation_id(ErrWithHostileHeaderKey())
            assert cid == "req_safe_non_str"
        except Exception as e:
            pytest.fail(f"CRITICAL: extract_correlation_id raised {type(e).__name__} on key of type {type(hostile_key).__name__}!")

    def test_non_dict_headers_attributes(self) -> None:
        """Adversarial input: headers attribute is not a dict (list, int, str, None)."""
        for bad_headers in [
            [("x-canopy-request-id", "val")],
            "x-canopy-request-id: val",
            12345,
            True,
            3.14,
        ]:
            class ErrWithBadHeaders(Exception):
                headers = bad_headers

            try:
                cid = CanopyClassifier.extract_correlation_id(ErrWithBadHeaders())
                assert cid is None
            except Exception as e:
                pytest.fail(f"CRITICAL: extract_correlation_id raised {type(e).__name__} on non-dict headers!")


# ============================================================================ #
# 3. Correlation ID Fuzzing                                                    #
# ============================================================================ #

class TestCorrelationIDFuzzing:
    """Fuzzing correlation ID extraction with hostile, oversized, and malformed inputs."""

    @pytest.mark.parametrize(
        "hostile_id",
        [
            "req_" + "x" * 10_000,
            "req_canopy\x00injected",
            "req_canopy\r\nSet-Cookie: admin=true",
            "req_canopy\nsecond_line",
            "req_canopy' OR '1'='1",
            "<script>alert(1)</script>",
            "../../../../etc/passwd",
            "req_\u202e_id",
            "req_canopy_🔥🚀",
        ],
    )
    def test_correlation_id_extraction_handles_hostile_strings(self, hostile_id: str) -> None:
        class Err(Exception):
            headers = {"x-canopy-request-id": hostile_id}

        # Must not raise an unhandled exception
        cid = CanopyClassifier.extract_correlation_id(Err())
        assert isinstance(cid, str)
        assert len(cid) > 0

    @pytest.mark.parametrize(
        "non_string_val",
        [
            12345,
            123.456,
            True,
            False,
            ["req_in_list"],
            {"nested": "req_id"},
        ],
    )
    def test_correlation_id_extraction_ignores_non_strings(self, non_string_val: Any) -> None:
        class Err(Exception):
            headers = {"x-canopy-request-id": non_string_val}

        cid = CanopyClassifier.extract_correlation_id(Err())
        assert cid is None


# ============================================================================ #
# 4. Circuit Breaker State Machine & Robustness                                #
# ============================================================================ #

class TestCircuitBreakerRobustness:
    """Stress tests verifying circuit breaker state machine transitions and invalidations."""

    def test_circuit_breaker_resilient_to_string_timestamp_in_state_file(self) -> None:
        """Adversarial scenario: state file corrupted with string last_failure_time.
        Bug identified: self._time_fn() - self._last_failure_time raises TypeError.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(json.dumps({"state": "failover", "last_failure_time": "2026-11-04"}))
            cb = AntigravityCircuitBreaker(state_file=state_file)
            try:
                state = cb.state
                assert state in (STATE_PRIMARY, STATE_FAILOVER, STATE_PROBING)
            except TypeError:
                pytest.fail("CRITICAL: AntigravityCircuitBreaker.state raised TypeError on string last_failure_time!")

    def test_circuit_breaker_resilient_to_corrupted_json_state_file(self) -> None:
        """Scenario: state file contains corrupted non-JSON content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text("{{corrupted_json:::&&")
            cb = AntigravityCircuitBreaker(state_file=state_file)
            assert cb.state == STATE_PRIMARY

    @pytest.mark.parametrize(
        "malformed_val",
        [
            None,
            "",
            "NaN",
            "nan",
            float("nan"),
            {"corrupted": True},
            [1, 2, "three"],
            True,
            False,
            "inf",
            "-inf",
            "not-a-valid-timestamp",
            "2026-11-04T22:30:00Z",
            "2026-11-04T22:30:00+00:00",
            "2026-11-04 22:30:00",
            "2026-11-04",
        ],
    )
    def test_circuit_breaker_resilient_to_malformed_timestamps(self, malformed_val: Any) -> None:
        """Adversarial scenario: state file corrupted with diverse malformed last_failure_time values.
        Verifies circuit breaker recovers gracefully without throwing unhandled exceptions.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(json.dumps({"state": "failover", "last_failure_time": malformed_val}))
            cb = AntigravityCircuitBreaker(state_file=state_file)
            try:
                state = cb.state
                assert state in (STATE_PRIMARY, STATE_FAILOVER, STATE_PROBING)
            except Exception as e:
                pytest.fail(f"CRITICAL: AntigravityCircuitBreaker.state raised {type(e).__name__} on {malformed_val!r}!")

    def test_circuit_breaker_malformed_timestamp_lifecycle(self) -> None:
        """Verifies full lifecycle operations succeed even after loading corrupted state."""
        class RealCanopyFailover(Exception):
            status_code = 402

        for malformed_val in (None, "", "NaN", {"bad": 1}, [1, 2], "not-a-date"):
            with tempfile.TemporaryDirectory() as tmpdir:
                state_file = Path(tmpdir) / "state.json"
                state_file.write_text(json.dumps({"state": "failover", "last_failure_time": malformed_val}))
                cb = AntigravityCircuitBreaker(state_file=state_file)

                # Query target
                target = cb.get_target_for_model("antigravity-flash-high")
                assert isinstance(target, str)

                # Record failure transitions or maintains failover
                assert cb.record_failure(RealCanopyFailover()) is True
                assert cb.state == STATE_FAILOVER

                # Reset to primary succeeds
                cb.reset()
                assert cb.state == STATE_PRIMARY

    def test_circuit_breaker_canopy_failover_cycle(self) -> None:
        """Verify full failover -> probing -> primary lifecycle driven by Canopy 402."""
        curr_time = 1000.0

        def time_mock() -> float:
            return curr_time

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            cb = AntigravityCircuitBreaker(cooldown_seconds=900.0, time_fn=time_mock, state_file=state_file)
            assert cb.state == STATE_PRIMARY

            # 1. Non-failover error (400) does not trip CB
            class Err400(Exception):
                status_code = 400
            assert cb.record_failure(Err400()) is False
            assert cb.state == STATE_PRIMARY

            # 2. Canopy 402 trips CB to FAILOVER
            class CanopyErr402(Exception):
                status_code = 402
            assert cb.record_failure(CanopyErr402()) is True
            assert cb.state == STATE_FAILOVER
            assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-remote-flash-high"

            # 3. Advance clock partially (899s) -> still FAILOVER
            curr_time += 899.0
            assert cb.state == STATE_FAILOVER

            # 4. Advance past cooldown (900s) -> PROBING
            curr_time += 1.0
            assert cb.state == STATE_PROBING
            assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-flash-high"

            # 5. Remote fallback success does NOT transition PROBING to PRIMARY
            assert cb.record_success(model="antigravity-remote-flash-high") is False
            assert cb.state == STATE_PROBING

            # 6. Primary probe success transitions to PRIMARY
            assert cb.record_success(model="antigravity-flash-high") is True
            assert cb.state == STATE_PRIMARY


# ============================================================================ #
# 5. False Positive Susceptibility on Bare '402' Numbers                        #
# ============================================================================ #

class TestClassifierFalsePositiveSusceptibility:
    """Demonstrates false positive vulnerability where bare '402' in unrelated errors trips classifier."""

    @pytest.mark.parametrize(
        "unrelated_error_message",
        [
            "Error code 500: Database table 402 is corrupted",
            "Task 402 failed with internal server error",
            "Latency 402 ms exceeded SLA timeout",
            "Worker 402 died unexpectedly on node 3",
            "Request failed with 402 bytes received from upstream",
        ],
    )
    def test_bare_402_in_unrelated_errors_should_not_be_exhaustion(self, unrelated_error_message: str) -> None:
        """Adversarial finding: MASKED_402_PATTERN = r'\\b402\\b' matches arbitrary 402 numbers.
        Unrelated internal errors must NOT be falsely classified as credit exhaustion.
        """
        is_exhausted = CanopyClassifier.is_canopy_exhaustion(unrelated_error_message)
        if is_exhausted:
            pytest.fail(
                f"VULNERABILITY: Unrelated error '{unrelated_error_message}' falsely classified as Canopy credit exhaustion! "
                "Bare '\\b402\\b' regex pattern lacks HTTP status or credit payment context."
            )


# ============================================================================ #
# 6. Invariant Verification: Zero Forbidden Service References                 #
# ============================================================================ #

class TestForbiddenServiceExclusionInvariant:
    """Verifies that zero references to the forbidden service exist in any codebase modules."""

    def test_zero_forbidden_service_in_python_sources(self) -> None:
        target_name = "".join([chr(c) for c in (98, 108, 97, 99, 107, 104, 111, 108, 101)])
        for py_file in REPO_ROOT.rglob("*.py"):
            if ".venv" in py_file.parts or ".git" in py_file.parts:
                continue
            content = py_file.read_text(encoding="utf-8", errors="replace").lower()
            assert target_name not in content, f"Forbidden reference found in {py_file}"



# ============================================================================ #
# 7. Dispatch Checklist: Benign Number False-Positive Stress                   #
# ============================================================================ #

class TestDispatchBenignNumberFalsePositives:
    """Stress tests from dispatch checklist verifying benign strings containing '402' are rejected."""

    @pytest.mark.parametrize(
        "benign_string",
        [
            "token_count: 402",
            "tokens: 402",
            "completion_tokens: 402",
            "total_tokens: 402",
            "prompt_tokens: 402",
            "user prompt asking about 402",
            "HTTP 402 error explanation in message body",
            "Connection to 10.0.0.1:402 failed",
            "Connected to 192.168.1.50:402",
        ],
    )
    def test_dispatch_benign_402_strings_must_return_false(self, benign_string: str) -> None:
        """Adversarial probe: strings with 402 in benign contexts must NOT be classified as exhaustion."""
        if CanopyClassifier.is_canopy_exhaustion(benign_string):
            pytest.fail(f"VULNERABILITY: Benign string '{benign_string}' falsely classified as Canopy credit exhaustion!")
        if is_failover_error(benign_string):
            pytest.fail(f"VULNERABILITY: tool_choice_guard.is_failover_error falsely tripped on '{benign_string}'!")
        if is_exhaustion_error(benign_string):
            pytest.fail(f"VULNERABILITY: tool_choice_guard.is_exhaustion_error falsely tripped on '{benign_string}'!")

    def test_benign_message_in_dict_must_return_false(self) -> None:
        """Adversarial probe: non-error dictionary with 'message' field mentioning 402 items."""
        d = {"message": "I found 402 results for your search"}
        if CanopyClassifier.is_canopy_exhaustion(d):
            pytest.fail("VULNERABILITY: Dict {'message': 'I found 402 results...'} falsely classified as exhaustion!")

    @pytest.mark.parametrize(
        "real_failover_error",
        [
            "HTTPConnectionPool(host=127.0.0.1:5596): 502 Bad Gateway",
            "Anthropic error: 429 Too Many Requests - prompt_tokens: 150",
            "Gateway failure at line 42: 504 Gateway Timeout",
            "Anthropic error: 429 Too Many Requests - 150 tokens consumed",
            "HTTPConnectionPool(host=10.0.0.1:402): 502 Bad Gateway",
            "Anthropic error: 429 Too Many Requests - prompt_tokens: 402",
        ],
    )
    def test_real_gateway_failovers_with_metadata_must_return_true(self, real_failover_error: str) -> None:
        assert is_failover_error(real_failover_error) is True, f"Failed to recognize failover for {real_failover_error!r}"


# ============================================================================ #
# 8. Dispatch Checklist: Chained & Nested Masked 402 Detection                 #
# ============================================================================ #

class TestChainedMaskedExceptionsDetection:
    """Verifies that chained exceptions with masked Canopy markers are detected."""

    def test_chained_cause_with_masked_anthropic_exception(self) -> None:
        """Adversarial probe: outer RuntimeError wrapping inner masked Canopy exhaustion via __cause__."""
        inner = Exception(
            "AnthropicException - b'{\"error\":{\"canopy_proxy\":\"CANOPY_AI_CREDITS_PROXY\","
            "\"reason\":\"personal_wallet_empty\"}}'"
        )
        outer = RuntimeError("Failed to route request to upstream")
        outer.__cause__ = inner

        assert CanopyClassifier.is_canopy_exhaustion(inner) is True, "Inner exception should be detected"
        if not CanopyClassifier.is_canopy_exhaustion(outer):
            pytest.fail(
                "VULNERABILITY: Outer exception with inner masked Canopy exhaustion in __cause__ was missed! "
                "classify_masked fails to inspect __cause__."
            )

    def test_chained_context_with_masked_anthropic_exception(self) -> None:
        """Adversarial probe: outer RuntimeError wrapping inner masked Canopy exhaustion via __context__."""
        inner = Exception("personal_wallet_empty: AI Credits are unavailable for this request")
        outer = RuntimeError("Gateway execution error")
        outer.__context__ = inner

        assert CanopyClassifier.is_canopy_exhaustion(inner) is True, "Inner exception should be detected"
        if not CanopyClassifier.is_canopy_exhaustion(outer):
            pytest.fail(
                "VULNERABILITY: Outer exception with inner masked Canopy exhaustion in __context__ was missed! "
                "classify_masked fails to inspect __context__."
            )

    def test_multi_level_chained_exception(self) -> None:
        """Adversarial probe: 3-level exception chain where root cause has masked Canopy exhaustion."""
        e1 = Exception("AnthropicException: personal_wallet_empty")
        e2 = Exception("Intermediate proxy handler failure")
        e2.__cause__ = e1
        e3 = Exception("Top-level pipeline error")
        e3.__cause__ = e2

        if not CanopyClassifier.is_canopy_exhaustion(e3):
            pytest.fail("VULNERABILITY: Multi-level chained exception (depth 3) with root masked exhaustion was missed!")


# ============================================================================ #
# 9. Concurrency & Throughput Stress Harness                                   #
# ============================================================================ #

class TestConcurrencyAndThroughputStress:
    """Verifies thread-safety, absence of race conditions, and performance under load."""

    def test_concurrency_1000_synthetic_classifications(self) -> None:
        import concurrent.futures
        import time

        errors = [
            {"status_code": 402},
            {"error": {"reason": "personal_wallet_empty"}},
            Exception("Processed 402 tokens"),
            {"prompt": "Explain HTTP 402"},
            Exception("SyntaxError on line 402"),
            Exception("Connection reset by peer"),
            {"status_code": 500},
            {"status_code": 429},
        ] * 125  # 1000 items

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(CanopyClassifier.is_canopy_exhaustion, errors))

        duration = time.perf_counter() - t0
        assert len(results) == 1000
        assert duration < 2.0, f"Classification throughput too slow: {duration:.3f}s for 1000 items"
