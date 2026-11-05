"""Comprehensive test suite for ErrorEnricher and Interface Contract 3.

Validates structured HTTP 402 payload synthesis, correlation ID extraction hierarchy,
CanopyResourceDepletedError exception properties, and critical invariants.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from service.agentic.endpoint_abstraction.error_enricher import (
    CORRELATION_ID_REGEX,
    DEFAULT_DEPLETED_MESSAGE,
    DEFAULT_ERROR_CODE,
    DEFAULT_ERROR_TYPE,
    DEFAULT_RESUME_INSTRUCTION,
    DEFAULT_STATUS,
    CanopyResourceDepletedError,
    ErrorEnricher,
)


# ============================================================================ #
# Tier 1: Feature Coverage & Contract Compliance                               #
# ============================================================================ #


def test_contract_payload_structure() -> None:
    """ErrorEnricher.build_payload creates exactly structured Interface Contract 3 dictionary."""
    payload = ErrorEnricher.build_payload()
    assert isinstance(payload, dict)
    assert "error" in payload
    err = payload["error"]

    expected_keys = {
        "message",
        "type",
        "code",
        "canopy_correlation_id",
        "status",
        "resume_instruction",
    }
    assert set(err.keys()) == expected_keys


def test_contract_default_values() -> None:
    """Verify default values strictly match Interface Contract 3."""
    payload = ErrorEnricher.build_payload()
    err = payload["error"]

    assert err["message"] == DEFAULT_DEPLETED_MESSAGE
    assert err["type"] == DEFAULT_ERROR_TYPE
    assert err["code"] == DEFAULT_ERROR_CODE
    assert err["status"] == DEFAULT_STATUS
    assert err["resume_instruction"] == DEFAULT_RESUME_INSTRUCTION
    assert CORRELATION_ID_REGEX.match(err["canopy_correlation_id"]) is not None


def test_contract_custom_overrides() -> None:
    """Custom arguments override defaults cleanly and merge extra fields."""
    payload = ErrorEnricher.build_payload(
        correlation_id="req_canopy_CUSTOM123",
        message="Custom depletion notice",
        resume_instruction="Custom restart instruction",
        status="paused_custom",
        error_type="insufficient_quota",
        code=402,
        extra_fields={"wallet_pool_size": 3, "retry_after": 60},
    )
    err = payload["error"]
    assert err["canopy_correlation_id"] == "req_canopy_CUSTOM123"
    assert err["message"] == "Custom depletion notice"
    assert err["resume_instruction"] == "Custom restart instruction"
    assert err["status"] == "paused_custom"
    assert err["type"] == "insufficient_quota"
    assert err["code"] == 402
    assert err["wallet_pool_size"] == 3
    assert err["retry_after"] == 60


def test_json_serialization() -> None:
    """build_json returns valid JSON string parseable back to identical dict."""
    json_str = ErrorEnricher.build_json(correlation_id="req_canopy_JSON001", indent=2)
    assert isinstance(json_str, str)
    decoded = json.loads(json_str)
    assert decoded["error"]["canopy_correlation_id"] == "req_canopy_JSON001"


def test_repr_and_dict_methods() -> None:
    """CanopyResourceDepletedError repr, to_dict, and to_json methods."""
    exc = ErrorEnricher.create_http_exception(correlation_id="req_canopy_REPR1")
    assert "CanopyResourceDepletedError" in repr(exc)
    assert "req_canopy_REPR1" in repr(exc)
    assert exc.to_dict() == exc.payload
    assert json.loads(exc.to_json()) == exc.payload


# ============================================================================ #
# Tier 2: Correlation ID Extraction & Fallback Generation                      #
# ============================================================================ #


def test_fallback_correlation_id_format_and_entropy() -> None:
    """Generated fallback IDs follow req_canopy_<8-char-hex> with high entropy."""
    generated_ids = [
        ErrorEnricher.generate_fallback_correlation_id() for _ in range(1000)
    ]
    for cid in generated_ids:
        assert cid.startswith("req_canopy_")
        assert len(cid) == 19  # 'req_canopy_' (11 chars) + 8 hex chars = 19
        assert CORRELATION_ID_REGEX.match(cid) is not None

    # Zero collisions among 1000 generated tokens
    assert len(set(generated_ids)) == 1000


def test_extract_from_case_insensitive_headers() -> None:
    """Correlation IDs are extracted from various candidate headers case-insensitively."""
    test_cases = [
        ({"X-Canopy-Correlation-Id": "cid_hdr_1"}, "cid_hdr_1"),
        ({"x-canopy-correlation-id": "cid_hdr_2"}, "cid_hdr_2"),
        ({"x-request-id": "cid_hdr_3"}, "cid_hdr_3"),
        ({"X-Request-Id": "cid_hdr_4"}, "cid_hdr_4"),
        ({"x-correlation-id": "cid_hdr_5"}, "cid_hdr_5"),
        ({"canopy-request-id": "cid_hdr_6"}, "cid_hdr_6"),
    ]
    for headers, expected in test_cases:
        extracted = ErrorEnricher.extract_correlation_id(headers=headers, fallback=False)
        assert extracted == expected


def test_extract_from_nested_request_data() -> None:
    """Extract from extra_headers, headers, metadata, or top-level keys in request dict."""
    data_1 = {"extra_headers": {"X-Request-ID": "req_nested_111"}}
    assert (
        ErrorEnricher.extract_correlation_id(source=data_1, fallback=False)
        == "req_nested_111"
    )

    data_2 = {"headers": {"x-canopy-correlation-id": "req_nested_222"}}
    assert (
        ErrorEnricher.extract_correlation_id(source=data_2, fallback=False)
        == "req_nested_222"
    )

    data_3 = {"metadata": {"correlation_id": "meta_cid_333"}}
    assert (
        ErrorEnricher.extract_correlation_id(source=data_3, fallback=False)
        == "meta_cid_333"
    )

    data_4 = {"canopy_correlation_id": "direct_cid_444"}
    assert (
        ErrorEnricher.extract_correlation_id(source=data_4, fallback=False)
        == "direct_cid_444"
    )


def test_extract_from_error_and_response_objects() -> None:
    """Extract from mock exception/response objects carrying headers."""
    mock_resp = MagicMock()
    mock_resp.headers = {"X-Canopy-Correlation-Id": "resp_obj_cid_555"}
    mock_err = MagicMock()
    mock_err.response = mock_resp

    extracted = ErrorEnricher.extract_correlation_id(source=mock_err, fallback=False)
    assert extracted == "resp_obj_cid_555"


def test_extract_from_pause_info_dict() -> None:
    """Extract correlation_id stored in pause info or lock payload."""
    pause_info = {
        "paused": True,
        "correlation_id": "req_lock_777",
        "reason": "All wallets exhausted",
    }
    extracted = ErrorEnricher.extract_correlation_id(source=pause_info, fallback=False)
    assert extracted == "req_lock_777"


def test_extract_from_serialized_json_string() -> None:
    """Extract from raw JSON error string payload."""
    json_str = '{"error": {"canopy_correlation_id": "str_json_888"}}'
    extracted = ErrorEnricher.extract_correlation_id(source=json_str, fallback=False)
    assert extracted == "str_json_888"


def test_extract_fallback_toggle() -> None:
    """When fallback=False and no ID found, returns None. When True, returns generated ID."""
    assert (
        ErrorEnricher.extract_correlation_id(source=None, headers=None, fallback=False)
        is None
    )
    fallback_id = ErrorEnricher.extract_correlation_id(
        source=None, headers=None, fallback=True
    )
    assert fallback_id is not None
    assert fallback_id.startswith("req_canopy_")


# ============================================================================ #
# Tier 3: Exception Mapping & LiteLLM Integration                              #
# ============================================================================ #


def test_canopy_resource_depleted_error_inheritance() -> None:
    """CanopyResourceDepletedError inherits from HTTPException with 402 status."""
    exc = ErrorEnricher.create_http_exception(correlation_id="req_canopy_EXC1")
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 402
    assert exc.correlation_id == "req_canopy_EXC1"
    assert exc.detail["code"] == 402
    assert exc.detail["canopy_correlation_id"] == "req_canopy_EXC1"


def test_http_exception_headers_and_status() -> None:
    """HTTPException includes X-Canopy-Correlation-Id and Content-Type headers."""
    exc = ErrorEnricher.create_http_exception(correlation_id="req_canopy_HDR1")
    assert exc.headers.get("X-Canopy-Correlation-Id") == "req_canopy_HDR1"
    assert exc.headers.get("Content-Type") == "application/json"


def test_litellm_api_error_synthesis() -> None:
    """create_litellm_exception synthesizes litellm APIError with payload attribute."""
    exc = ErrorEnricher.create_litellm_exception(
        correlation_id="req_canopy_LITE1",
        model="deepseek-v4-pro",
    )
    assert getattr(exc, "status_code", None) == 402
    assert hasattr(exc, "payload")
    assert exc.payload["error"]["canopy_correlation_id"] == "req_canopy_LITE1"


def test_format_pause_exception_flow() -> None:
    """format_pause_exception extracts correlation ID from error or request."""
    mock_err = MagicMock()
    mock_err.headers = {"x-canopy-correlation-id": "req_err_flow_99"}

    exc = ErrorEnricher.format_pause_exception(
        error=mock_err,
        request_data={"extra_headers": {"x-request-id": "req_fallback_flow"}},
    )
    assert exc.correlation_id == "req_err_flow_99"


def test_format_fast_fail_flow() -> None:
    """format_fast_fail extracts correlation ID from pause_info when fast-failing."""
    pause_info = {"correlation_id": "req_lock_fast_fail_1", "paused": True}
    exc = ErrorEnricher.format_fast_fail(
        request_data={"model": "deepseek-v4-pro"},
        pause_info=pause_info,
    )
    assert exc.correlation_id == "req_lock_fast_fail_1"


def test_format_http_exception_alias() -> None:
    """format_http_exception provides compatible signature for hook calls."""
    exc = ErrorEnricher.format_http_exception(
        correlation_id="req_alias_1",
        reason="personal_wallet_empty",
    )
    assert exc.status_code == 402
    assert exc.correlation_id == "req_alias_1"


# ============================================================================ #
# Tier 4: Boundary, Corner Cases & Invariant Verification                      #
# ============================================================================ #


def test_corrupt_and_non_dict_inputs() -> None:
    """Non-dict and unusual inputs do not crash extract_correlation_id or build_payload."""
    assert ErrorEnricher.extract_correlation_id(source=12345, fallback=False) is None
    assert (
        ErrorEnricher.extract_correlation_id(source=["a", "b"], fallback=False) is None
    )

    payload = ErrorEnricher.build_payload(extra_fields="not-a-dict")  # type: ignore[arg-type]
    assert "error" in payload


def test_whitespace_and_empty_headers() -> None:
    """Whitespace-only headers are ignored in favor of fallback."""
    headers = {"x-canopy-correlation-id": "   ", "x-request-id": ""}
    extracted = ErrorEnricher.extract_correlation_id(headers=headers, fallback=False)
    assert extracted is None


def test_special_characters_in_correlation_ids() -> None:
    """Correlation IDs with UUIDs, dashes, and underscores are preserved verbatim."""
    cid_special = "req_canopy_uuid-1234-5678_alpha"
    extracted = ErrorEnricher.extract_correlation_id(source=cid_special, fallback=False)
    assert extracted == cid_special


def test_zero_forbidden_words_in_source_and_tests() -> None:
    """Audit source and test files to guarantee ZERO forbidden terms exist."""
    forbidden = "".join(["black", "hole"])
    source_path = (
        Path(__file__).resolve().parent.parent
        / "service"
        / "agentic"
        / "endpoint_abstraction"
        / "error_enricher.py"
    )
    test_path = Path(__file__).resolve()

    pattern = re.compile(forbidden, re.IGNORECASE)
    assert not pattern.search(
        source_path.read_text(encoding="utf-8")
    ), f"Forbidden term found in {source_path}"
    assert not pattern.search(
        test_path.read_text(encoding="utf-8")
    ), f"Forbidden term found in {test_path}"
