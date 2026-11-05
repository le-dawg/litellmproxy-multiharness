"""Unit test suite for Canopy 402 Classifier & tool_choice_guard integration.

Scope: Phase 1 Task 1.3 (Milestone 1: Classification & Hardening)
Test Runner: uv run pytest tests/test_canopy_classifier.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import httpx
import litellm
import pytest
from fastapi import HTTPException

# Add ~/.litellm and repository root to python path (REPO_ROOT takes priority)
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path.home() / ".litellm"))
sys.path.insert(0, str(REPO_ROOT))

from service.agentic.endpoint_abstraction.canopy_classifier import (
    CanopyClassificationResult,
    CanopyClassifier,
    extract_canopy_correlation_id,
    extract_canopy_reason,
    is_canopy_exhaustion,
)

from tool_choice_guard import (
    EXHAUSTION_PHRASES,
    FAILOVER_STATUS_CODES,
    is_exhaustion_error,
    is_failover_error,
)


# ============================================================================ #
# Test Fixtures & Mock Helpers                                                 #
# ============================================================================ #

class MockResponse:
    """Mock HTTP response object mimicking httpx.Response / requests.Response."""

    def __init__(
        self,
        status_code: int = 402,
        text: str = "",
        json_data: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.status_code = status_code
        self.status = status_code
        self._json_data = json_data
        self.text = text if text else (json.dumps(json_data) if json_data is not None else "")
        self.headers = headers or {}

    def json(self) -> Any:
        if self._json_data is not None:
            return self._json_data
        if self.text:
            return json.loads(self.text)
        return {}


class DummyHTTPError(Exception):
    """Exception carrying status_code and optional response object."""

    def __init__(
        self,
        msg: str,
        status_code: Any = 402,
        response: Any = None,
        headers: Any = None,
    ) -> None:
        super().__init__(msg)
        self.status_code = status_code
        self.response = response
        self.headers = headers


class MaskedHTTPStatusError(Exception):
    """Exception simulating upstream error masked with status 500."""

    def __init__(self, message: str, status_code: int = 500, response: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response


VERBATIM_CANOPY_PAYLOAD: Dict[str, Any] = {
    "error": {
        "canopy_proxy": "CANOPY_AI_CREDITS_PROXY",
        "code": "no_billing_source",
        "correlation_id": "aadfc148-2721-4b7f-81e9-f9b0b9dbbdff",
        "reason": "personal_wallet_empty",
        "message": "AI Credits are unavailable for this request.",
    }
}

VERBATIM_CANOPY_RAW_JSON: str = json.dumps(VERBATIM_CANOPY_PAYLOAD)

VERBATIM_LITELLM_API_CONNECTION_ERROR_STRING: str = (
    "litellm.exceptions.APIConnectionError: AnthropicException - "
    "b'{\"error\":{\"canopy_proxy\":\"CANOPY_AI_CREDITS_PROXY\",\"code\":\"no_billing_source\","
    "\"correlation_id\":\"b2293940-1aed-4b86-b0ed-87d9abfce229\",\"reason\":\"personal_wallet_empty\","
    "\"message\":\"AI Credits are unavailable for this request.\"}}'"
)


# ============================================================================ #
# 1. Status Layer Detection Tests                                              #
# ============================================================================ #

class TestCanopyStatusLayer:
    """Verifies direct HTTP 402 status detection across various exception types."""

    def test_status_code_int_402(self) -> None:
        err = DummyHTTPError("Payment Required", status_code=402)
        assert CanopyClassifier.is_canopy_exhaustion(err) is True
        assert is_canopy_exhaustion(err) is True

    def test_status_code_str_402(self) -> None:
        err = DummyHTTPError("Payment Required", status_code="402")
        assert CanopyClassifier.is_canopy_exhaustion(err) is True

    def test_http_status_attribute_402(self) -> None:
        class HTTPStatusObj(Exception):
            http_status = 402
        assert CanopyClassifier.is_canopy_exhaustion(HTTPStatusObj("msg")) is True

    def test_code_attribute_402(self) -> None:
        class CodeObj(Exception):
            code = 402
        assert CanopyClassifier.is_canopy_exhaustion(CodeObj("msg")) is True

    def test_wrapped_response_status_code_402(self) -> None:
        resp = MockResponse(status_code=402)
        err = DummyHTTPError("Upstream error", status_code=500, response=resp)
        assert CanopyClassifier.is_canopy_exhaustion(err) is True

    def test_httpx_http_status_error_402(self) -> None:
        req = httpx.Request("POST", "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic/v1/messages")
        resp = httpx.Response(402, request=req, content=b"Payment Required")
        err = httpx.HTTPStatusError("Client error '402 Payment Required'", request=req, response=resp)
        assert CanopyClassifier.is_canopy_exhaustion(err) is True

    def test_fastapi_http_exception_402(self) -> None:
        exc = HTTPException(status_code=402, detail="Payment Required")
        assert CanopyClassifier.is_canopy_exhaustion(exc) is True

    @pytest.mark.parametrize("status_key,val", [
        ("status_code", 402),
        ("status_code", "402"),
        ("code", 402),
        ("code", "402"),
        ("error_code", 402),
        ("status", "402"),
    ])
    def test_dict_status_code_402(self, status_key: str, val: Any) -> None:
        payload = {status_key: val}
        assert CanopyClassifier.is_canopy_exhaustion(payload) is True


# ============================================================================ #
# 2. JSON Payload Layer Detection Tests                                        #
# ============================================================================ #

class TestCanopyPayloadLayer:
    """Verifies detection of structured Canopy error payloads."""

    def test_verbatim_canopy_payload_dict(self) -> None:
        assert CanopyClassifier.is_canopy_exhaustion(VERBATIM_CANOPY_PAYLOAD) is True

    def test_nested_error_reason_personal_wallet_empty(self) -> None:
        d = {"error": {"reason": "personal_wallet_empty"}}
        assert CanopyClassifier.is_canopy_exhaustion(d) is True

    def test_nested_error_code_no_billing_source(self) -> None:
        d = {"error": {"code": "no_billing_source"}}
        assert CanopyClassifier.is_canopy_exhaustion(d) is True

    def test_nested_error_canopy_proxy_marker(self) -> None:
        d = {"error": {"canopy_proxy": "CANOPY_AI_CREDITS_PROXY"}}
        assert CanopyClassifier.is_canopy_exhaustion(d) is True

    def test_flat_dict_markers(self) -> None:
        assert CanopyClassifier.is_canopy_exhaustion({"reason": "personal_wallet_empty"}) is True
        assert CanopyClassifier.is_canopy_exhaustion({"code": "no_billing_source"}) is True
        assert CanopyClassifier.is_canopy_exhaustion({"canopy_proxy": "CANOPY_AI_CREDITS_PROXY"}) is True

    def test_serialized_json_string(self) -> None:
        assert CanopyClassifier.is_canopy_exhaustion(VERBATIM_CANOPY_RAW_JSON) is True

    def test_exception_with_response_json_callable(self) -> None:
        resp = MockResponse(status_code=200, json_data=VERBATIM_CANOPY_PAYLOAD)
        err = DummyHTTPError("Failed", status_code=None, response=resp)
        assert CanopyClassifier.is_canopy_exhaustion(err) is True

    def test_exception_with_response_text_property(self) -> None:
        resp = MockResponse(status_code=200, text=VERBATIM_CANOPY_RAW_JSON)
        err = DummyHTTPError("Failed", status_code=None, response=resp)
        assert CanopyClassifier.is_canopy_exhaustion(err) is True

    def test_case_insensitive_markers(self) -> None:
        assert CanopyClassifier.is_canopy_exhaustion({"reason": "PERSONAL_WALLET_EMPTY"}) is True
        assert CanopyClassifier.is_canopy_exhaustion({"code": "NO_BILLING_SOURCE"}) is True
        assert CanopyClassifier.is_canopy_exhaustion({"canopy_proxy": "canopy_ai_credits_proxy"}) is True


# ============================================================================ #
# 3. Masked Exception Layer Detection Tests                                    #
# ============================================================================ #

class TestCanopyMaskedLayer:
    """Verifies regex matching on masked exceptions and APIConnectionError."""

    def test_litellm_api_connection_error_anthropic_exception(self) -> None:
        err = litellm.exceptions.APIConnectionError(
            VERBATIM_LITELLM_API_CONNECTION_ERROR_STRING,
            llm_provider="anthropic",
            model="deepseek-v4-pro",
        )
        assert CanopyClassifier.is_canopy_exhaustion(err) is True

    def test_masked_http_status_error_500(self) -> None:
        err = MaskedHTTPStatusError("HTTP 500: AnthropicException - personal_wallet_empty", status_code=500)
        assert CanopyClassifier.is_canopy_exhaustion(err) is True

    @pytest.mark.parametrize("phrase", [
        "Payment Required: upstream credits depleted",
        "client error: payment_required",
        "PAYMENT REQUIRED",
        "payment_required",
    ])
    def test_regex_payment_required_phrases(self, phrase: str) -> None:
        assert CanopyClassifier.is_canopy_exhaustion(Exception(phrase)) is True
        assert CanopyClassifier.is_canopy_exhaustion(phrase) is True

    @pytest.mark.parametrize("phrase", [
        "AI Credits are unavailable for this request.",
        "upstream reported: ai credits are unavailable",
        "Notice: AI CREDITS ARE UNAVAILABLE",
    ])
    def test_regex_ai_credits_are_unavailable_phrases(self, phrase: str) -> None:
        assert CanopyClassifier.is_canopy_exhaustion(Exception(phrase)) is True
        assert CanopyClassifier.is_canopy_exhaustion(phrase) is True

    @pytest.mark.parametrize("phrase", [
        "HTTP 402 from upstream",
        "status_code: 402",
        "status: 402 Payment Required",
        "code: 402, reason: credits exhausted",
    ])
    def test_regex_status_402_markers(self, phrase: str) -> None:
        assert CanopyClassifier.is_canopy_exhaustion(Exception(phrase)) is True

    def test_nested_exception_args(self) -> None:
        inner = Exception("AnthropicException - personal_wallet_empty")
        outer = Exception("Outer wrapper", inner)
        assert CanopyClassifier.is_canopy_exhaustion(outer) is True

    def test_bytes_repr_in_str(self) -> None:
        err = Exception("b'{\"error\":{\"reason\":\"personal_wallet_empty\"}}'")
        assert CanopyClassifier.is_canopy_exhaustion(err) is True


# ============================================================================ #
# 4. Non-Canopy Error Rejection Tests (Negative Tests)                         #
# ============================================================================ #

class TestCanopyRejection:
    """Verifies prevention of false positives on non-Canopy errors and benign numbers."""

    @pytest.mark.parametrize("benign_err", [
        Exception("Processed 402 tokens in 1.2s"),
        Exception("Connection failed to 127.0.0.1:4020"),
        Exception("SyntaxError on line 402"),
        Exception("Port 402 is closed"),
        {"prompt": "Can you explain HTTP status code 402?"},
        {"content": "Explain 402 payment required error"},
    ])
    def test_reject_benign_402_numbers(self, benign_err: Any) -> None:
        assert CanopyClassifier.is_canopy_exhaustion(benign_err) is False

    def test_reject_rate_limit_429(self) -> None:
        err = DummyHTTPError("Rate Limit", status_code=429)
        assert CanopyClassifier.is_canopy_exhaustion(err) is False

    @pytest.mark.parametrize("code,msg", [
        (500, "Internal Server Error"),
        (502, "Bad Gateway"),
        (504, "Gateway Timeout"),
    ])
    def test_reject_gateway_errors_without_markers(self, code: int, msg: str) -> None:
        assert CanopyClassifier.is_canopy_exhaustion(DummyHTTPError(msg, code)) is False

    def test_reject_litellm_bad_gateway_error(self) -> None:
        err = litellm.exceptions.BadGatewayError("Bad Gateway from upstream", "deepseek-v4-pro", "openai")
        assert CanopyClassifier.is_canopy_exhaustion(err) is False

    @pytest.mark.parametrize("payload", [
        {"error": "LLM proxy requests must set stream: true."},
        {"error": "Invalid API key"},
    ])
    def test_reject_canopy_non_exhaustion_responses(self, payload: Dict[str, Any]) -> None:
        assert CanopyClassifier.is_canopy_exhaustion(payload) is False

    @pytest.mark.parametrize("net_err", [
        httpx.ConnectError("Connection refused"),
        httpx.ReadTimeout("Read timed out"),
        litellm.exceptions.APIConnectionError("Connection reset by peer", "anthropic", "deepseek-v4-pro"),
    ])
    def test_reject_general_network_errors(self, net_err: Exception) -> None:
        assert CanopyClassifier.is_canopy_exhaustion(net_err) is False

    @pytest.mark.parametrize("val", [
        None,
        "",
        "   ",
        {},
        [],
        123,
        True,
        ValueError("invalid value"),
        KeyError("missing_key"),
    ])
    def test_reject_python_builtins_and_empty(self, val: Any) -> None:
        assert CanopyClassifier.is_canopy_exhaustion(val) is False


# ============================================================================ #
# 5. Correlation ID Extraction Tests                                           #
# ============================================================================ #

class TestCanopyCorrelationIdExtraction:
    """Verifies extraction of Canopy correlation IDs from payloads and headers."""

    def test_extract_correlation_id_nested_json(self) -> None:
        cid = CanopyClassifier.extract_correlation_id(VERBATIM_CANOPY_PAYLOAD)
        assert cid == "aadfc148-2721-4b7f-81e9-f9b0b9dbbdff"
        assert extract_canopy_correlation_id(VERBATIM_CANOPY_PAYLOAD) == "aadfc148-2721-4b7f-81e9-f9b0b9dbbdff"

    def test_extract_correlation_id_flat_dict(self) -> None:
        d = {"correlation_id": "req_canopy_01ABCDEF"}
        assert CanopyClassifier.extract_correlation_id(d) == "req_canopy_01ABCDEF"

    def test_extract_correlation_id_canopy_prefix_key(self) -> None:
        d = {"canopy_correlation_id": "req_canopy_custom_123"}
        assert CanopyClassifier.extract_correlation_id(d) == "req_canopy_custom_123"

    def test_extract_correlation_id_header_x_canopy_request_id(self) -> None:
        headers = {"x-canopy-request-id": "hdr-canopy-12345"}
        resp = MockResponse(status_code=402, headers=headers)
        err = DummyHTTPError("Payment Required", status_code=402, response=resp)
        assert CanopyClassifier.extract_correlation_id(err) == "hdr-canopy-12345"

    def test_extract_correlation_id_header_x_request_id(self) -> None:
        headers = {"x-request-id": "hdr-req-67890"}
        resp = MockResponse(status_code=402, headers=headers)
        err = DummyHTTPError("Payment Required", status_code=402, response=resp)
        assert CanopyClassifier.extract_correlation_id(err) == "hdr-req-67890"

    def test_extract_correlation_id_serialized_json(self) -> None:
        s = '{"error": {"correlation_id": "str-canopy-999"}}'
        assert CanopyClassifier.extract_correlation_id(s) == "str-canopy-999"

    def test_extract_correlation_id_litellm_connection_error(self) -> None:
        err = litellm.exceptions.APIConnectionError(
            VERBATIM_LITELLM_API_CONNECTION_ERROR_STRING,
            llm_provider="anthropic",
            model="deepseek-v4-pro",
        )
        assert CanopyClassifier.extract_correlation_id(err) == "b2293940-1aed-4b86-b0ed-87d9abfce229"

    def test_extract_correlation_id_missing_returns_none(self) -> None:
        d = {"error": {"reason": "personal_wallet_empty"}}
        assert CanopyClassifier.extract_correlation_id(d) is None

    @pytest.mark.parametrize("val", [None, "", "   ", Exception("generic")])
    def test_extract_correlation_id_benign_returns_none(self, val: Any) -> None:
        assert CanopyClassifier.extract_correlation_id(val) is None


# ============================================================================ #
# 6. Exhaustion Reason Extraction Tests                                        #
# ============================================================================ #

class TestCanopyReasonExtraction:
    """Verifies extraction of exhaustion reason tokens."""

    def test_extract_reason_personal_wallet_empty(self) -> None:
        reason = CanopyClassifier.extract_exhaustion_reason(VERBATIM_CANOPY_PAYLOAD)
        assert reason == "personal_wallet_empty"
        assert extract_canopy_reason(VERBATIM_CANOPY_PAYLOAD) == "personal_wallet_empty"

    def test_extract_reason_no_billing_source(self) -> None:
        d = {"error": {"code": "no_billing_source"}}
        assert CanopyClassifier.extract_exhaustion_reason(d) == "no_billing_source"

    def test_extract_reason_string_fallback(self) -> None:
        err = Exception("AnthropicException - personal_wallet_empty")
        assert CanopyClassifier.extract_exhaustion_reason(err) == "personal_wallet_empty"

    @pytest.mark.parametrize("val", [None, "", Exception("generic")])
    def test_extract_reason_none_for_benign(self, val: Any) -> None:
        assert CanopyClassifier.extract_exhaustion_reason(val) is None


# ============================================================================ #
# 7. Classification Result Dataclass Tests                                    #
# ============================================================================ #

class TestCanopyClassificationResult:
    """Verifies fields and behavior of CanopyClassificationResult dataclass."""

    def test_classification_result_payload_tier(self) -> None:
        res = CanopyClassifier.classify(VERBATIM_CANOPY_PAYLOAD)
        assert isinstance(res, CanopyClassificationResult)
        assert res.is_exhausted is True
        assert res.matched_tier == "payload"
        assert res.correlation_id == "aadfc148-2721-4b7f-81e9-f9b0b9dbbdff"
        assert res.details.get("reason") == "personal_wallet_empty"
        assert res.details.get("code") == "no_billing_source"

    def test_classification_result_status_tier(self) -> None:
        err = DummyHTTPError("Payment Required", status_code=402)
        res = CanopyClassifier.classify(err)
        assert res.is_exhausted is True
        assert res.matched_tier == "status"

    def test_classification_result_masked_tier(self) -> None:
        err = Exception("Payment Required: upstream credits depleted")
        res = CanopyClassifier.classify(err)
        assert res.is_exhausted is True
        assert res.matched_tier == "masked"

    def test_classification_result_benign(self) -> None:
        res = CanopyClassifier.classify("Normal response")
        assert res.is_exhausted is False
        assert res.matched_tier is None


# ============================================================================ #
# 8. Integration with tool_choice_guard.py Tests                               #
# ============================================================================ #

class TestToolChoiceGuardIntegration:
    """Verifies that tool_choice_guard circuit breaker functions incorporate Canopy markers."""

    def test_guard_failover_status_codes_includes_402(self) -> None:
        assert 402 in FAILOVER_STATUS_CODES
        assert "402" in FAILOVER_STATUS_CODES

    def test_guard_exhaustion_phrases_includes_canopy_markers(self) -> None:
        expected_markers = {
            "personal_wallet_empty",
            "no_billing_source",
            "payment required",
            "ai credits are unavailable",
            "canopy_ai_credits_proxy",
        }
        for marker in expected_markers:
            assert marker in EXHAUSTION_PHRASES, f"Missing Canopy marker in EXHAUSTION_PHRASES: {marker}"

    def test_is_failover_error_detects_canopy_402_status(self) -> None:
        assert is_failover_error(DummyHTTPError("Payment Required", 402)) is True
        assert is_failover_error(DummyHTTPError("Payment Required", "402")) is True

    def test_is_failover_error_detects_canopy_json_payload(self) -> None:
        assert is_failover_error(VERBATIM_CANOPY_PAYLOAD) is True
        assert is_failover_error(VERBATIM_CANOPY_RAW_JSON) is True

    def test_is_failover_error_detects_canopy_api_connection_error(self) -> None:
        err = litellm.exceptions.APIConnectionError(
            VERBATIM_LITELLM_API_CONNECTION_ERROR_STRING,
            llm_provider="anthropic",
            model="deepseek-v4-pro",
        )
        assert is_failover_error(err) is True

    def test_is_exhaustion_error_detects_canopy_errors(self) -> None:
        assert is_exhaustion_error(DummyHTTPError("Payment Required", 402)) is True
        assert is_exhaustion_error(VERBATIM_CANOPY_PAYLOAD) is True
        assert is_exhaustion_error(VERBATIM_LITELLM_API_CONNECTION_ERROR_STRING) is True

    def test_is_failover_error_preserves_existing_429_502_504(self) -> None:
        assert is_failover_error(DummyHTTPError("Rate Limit", 429)) is True
        assert is_failover_error(DummyHTTPError("Bad Gateway", 502)) is True
        assert is_failover_error(DummyHTTPError("Gateway Timeout", 504)) is True
        assert is_failover_error("502 bad gateway") is True
        assert is_failover_error("quota exceeded") is True

    @pytest.mark.parametrize("benign_err", [
        Exception("Processed 402 tokens in 1.2s"),
        Exception("Connection failed to 127.0.0.1:4020"),
        Exception("SyntaxError on line 402"),
        {"prompt": "Can you explain HTTP status code 402?"},
        {"content": "Explain 402 payment required error"},
    ])
    def test_is_failover_error_rejects_benign_402_numbers(self, benign_err: Any) -> None:
        assert is_failover_error(benign_err) is False


# ============================================================================ #
# 9. Regression Locking: Benign Number Filter Precision Tests                  #
# ============================================================================ #

class TestCanopyBenignFilterRegression:
    """Regression-locking tests for CanopyClassifier and tool_choice_guard benign filtering.

    Ensures that generic token counts in upstream errors (e.g. consumed 50 tokens)
    do not suppress Tier 3 masked 402 classification, and verifies all Reviewer 1 cases.
    """

    @pytest.mark.parametrize(
        "error_str",
        [
            "Canopy error: 402 Payment Required (consumed 50 tokens)",
            "Canopy error: status 402 (consumed 50 tokens)",
            "AnthropicException: status 402 - personal_wallet_empty (consumed 1250 tokens)",
            "Canopy error: 402 payment required - proxy at 127.0.0.1:5596",
        ],
    )
    def test_canopy_classifier_detects_402_with_arbitrary_token_counts(self, error_str: str) -> None:
        """CanopyClassifier must detect 402 exhaustion even when error details token counts."""
        assert CanopyClassifier.is_canopy_exhaustion(error_str) is True
        assert CanopyClassifier.is_canopy_exhaustion(Exception(error_str)) is True

    @pytest.mark.parametrize(
        "non_canopy_failover",
        [
            "HTTPConnectionPool(host=127.0.0.1:5596): 502 Bad Gateway",
            "Gateway failure at line 42: 504 Gateway Timeout",
            "Anthropic error: 429 Too Many Requests - prompt_tokens: 150, completion_tokens: 25",
            "Upstream proxy failure at 192.168.1.100:8000: 502 Bad Gateway",
        ],
    )
    def test_canopy_classifier_rejects_non_canopy_gateway_errors(self, non_canopy_failover: str) -> None:
        """CanopyClassifier should only identify Canopy 402 exhaustion, rejecting generic 502/504/429."""
        assert CanopyClassifier.is_canopy_exhaustion(non_canopy_failover) is False
        assert CanopyClassifier.is_canopy_exhaustion(Exception(non_canopy_failover)) is False

    @pytest.mark.parametrize(
        "reviewer_case,expected_failover",
        [
            ("HTTPConnectionPool(host=127.0.0.1:5596): 502 Bad Gateway", True),
            ("Anthropic error: 429 Too Many Requests - prompt_tokens: 150, completion_tokens: 25", True),
            ("Gateway failure at line 42: 504 Gateway Timeout", True),
            ("Upstream proxy failure at 192.168.1.100:8000: 502 Bad Gateway", True),
            ("Canopy error: 402 Payment Required (consumed 50 tokens)", True),
        ],
    )
    def test_reviewer_1_regression_cases_all_evaluate_true(
        self, reviewer_case: str, expected_failover: bool
    ) -> None:
        """Verifies all five Reviewer 1 regression cases evaluate to True in is_failover_error."""
        assert is_failover_error(reviewer_case) is expected_failover
        assert is_failover_error(Exception(reviewer_case)) is expected_failover

    @pytest.mark.parametrize(
        "benign_mention",
        [
            "token_count: 402",
            "Connected to 10.0.0.1:402",
            "explanation of 402",
            "SyntaxError on line 402",
            "user prompt asking about 402",
        ],
    )
    def test_benign_mentions_evaluate_false_in_both(self, benign_mention: str) -> None:
        """Benign 402 mentions must be rejected by both CanopyClassifier and is_failover_error."""
        assert CanopyClassifier.is_canopy_exhaustion(benign_mention) is False
        assert is_failover_error(benign_mention) is False

