"""Multi-Tier Canopy 402 Error Classifier and Correlation ID Parser.

Provides deterministic detection of Upstream Canopy Proxy credit exhaustion
across three layers:
1. Status Layer: Raw HTTP 402 status codes, HTTPException, status_code/code attributes.
2. Payload Layer: JSON body inspection (canopy_proxy, reason=personal_wallet_empty, code=no_billing_source).
3. Masked Layer: Regex matching on masked exceptions (APIConnectionError, MaskedHTTPStatusError, string messages).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# Upstream Canopy Proxy constants
CANOPY_PROXY_IDENTIFIER = "CANOPY_AI_CREDITS_PROXY"
CANOPY_REASON_WALLET_EMPTY = "personal_wallet_empty"
CANOPY_CODE_NO_BILLING = "no_billing_source"

CANOPY_STATUS_CODES: Tuple[Union[int, str], ...] = (402, "402")

CANOPY_EXHAUSTION_PHRASES: Tuple[str, ...] = (
    "personal_wallet_empty",
    "canopy_ai_credits_proxy",
    "no_billing_source",
    "ai credits are unavailable for this request",
    "ai credits are unavailable",
    "payment required",
    "payment_required",
)

# Regex patterns for Tier 3 Masked Layer
MASKED_402_PATTERN = re.compile(
    r"\b(?:status(?:_code)?|http|code|error|err)\s*[:=]?\s*402\b|\b402\s+(?:payment[_\s]required|payment|credits?|wallet|exhausted)\b",
    re.IGNORECASE,
)
MASKED_PAYMENT_REQUIRED_PATTERN = re.compile(r"\bpayment[_\s]required\b", re.IGNORECASE)
MASKED_CREDITS_UNAVAILABLE_PATTERN = re.compile(r"\bai credits are unavailable\b", re.IGNORECASE)
MASKED_CANOPY_MARKERS_PATTERN = re.compile(
    r"\b(?:personal_wallet_empty|canopy_ai_credits_proxy|no_billing_source)\b",
    re.IGNORECASE,
)

# Regex patterns for correlation ID extraction
CORRELATION_ID_JSON_PATTERN = re.compile(
    r'["\'](?:canopy_)?correlation_id["\']\s*:\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
CORRELATION_ID_HEADER_PATTERN = re.compile(
    r'(?:canopy[_-])?correlation[_-]id\s*[:=]\s*["\']?([a-zA-Z0-9_\-]+)["\']?',
    re.IGNORECASE,
)

# False positive guardrails (reject token counts, line numbers, port numbers, latency, table/task IDs, explanations)
BENIGN_NUMBER_PATTERNS = (
    re.compile(r"\bline\s+402\b", re.IGNORECASE),
    re.compile(r"\b(?:(?:prompt_|completion_|total_)?tokens?|token_count)\s*[:=]?\s*402\b", re.IGNORECASE),
    re.compile(r"\b402\s+tokens?\b", re.IGNORECASE),
    re.compile(r"\b(?:latency|duration|elapsed|timeout)\s*[:=]?\s*402\b", re.IGNORECASE),
    re.compile(r"\b402\s*(?:ms|milliseconds?|s|seconds?)\b", re.IGNORECASE),
    re.compile(r"\b402\s*(?:bytes?|kb|mb|gb|b)\b", re.IGNORECASE),
    re.compile(r"\b(?:table|task|worker|job|node|thread|process|record|item|row)\s+402\b", re.IGNORECASE),
    re.compile(r"\b402\s+(?:results?|items?|records?|rows?)\b", re.IGNORECASE),
    re.compile(r"\bport\s+402\d*\b", re.IGNORECASE),
    re.compile(r":402\d*\b"),
    re.compile(r"\b(?:user\s+prompt|prompt|asking\s+about|explanation|explaining)\b", re.IGNORECASE),
)

REQUEST_PAYLOAD_KEYS = {"prompt", "content", "messages", "input", "query"}


@dataclass(frozen=True)
class CanopyClassificationResult:
    """Detailed result of Canopy 402 credit exhaustion classification."""

    is_exhausted: bool
    matched_tier: Optional[str] = None  # "status", "payload", "masked", or None
    correlation_id: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


class CanopyClassifier:
    """Deterministic 3-tier classifier for Canopy Proxy credit exhaustion errors."""

    @classmethod
    def classify_status(
        cls,
        error: Any,
        seen: Optional[Set[int]] = None,
        depth: int = 0,
    ) -> bool:
        """Tier 1: Check status layer for HTTP 402 or '402'."""
        if error is None or error == "" or isinstance(error, bool) or depth > 10:
            return False

        if seen is not None and id(error) in seen:
            return False

        # Raw status code match
        if error in CANOPY_STATUS_CODES:
            return True

        # Reject standard non-HTTP programming errors unless status attributes present
        if isinstance(error, (SyntaxError, IndentationError, TabError, NameError, TypeError, KeyError, ValueError)):
            if not any(hasattr(error, attr) for attr in ("status_code", "http_status", "code", "error_code", "status")):
                return False

        # Attribute inspection on exception or object
        for attr in ("status_code", "http_status", "code", "error_code", "status"):
            val = getattr(error, attr, None)
            if val in CANOPY_STATUS_CODES:
                return True

        # Response attribute (e.g. httpx.HTTPStatusError, MaskedHTTPStatusError, requests.HTTPError)
        response = getattr(error, "response", None)
        if response is not None:
            resp_status = getattr(response, "status_code", None) or getattr(response, "status", None)
            if resp_status in CANOPY_STATUS_CODES:
                return True

        child_seen = set(seen) if seen is not None else set()
        child_seen.add(id(error))

        # Dictionary inspection
        if isinstance(error, dict):
            for key in ("status_code", "http_status", "code", "error_code", "status"):
                if error.get(key) in CANOPY_STATUS_CODES:
                    return True
            err_sub = error.get("error")
            if isinstance(err_sub, dict):
                for key in ("status_code", "http_status", "code", "error_code", "status"):
                    if err_sub.get(key) in CANOPY_STATUS_CODES:
                        return True
                if cls.classify_status(err_sub, seen=child_seen, depth=depth + 1):
                    return True

        # Recursive check on nested exception causes (__cause__ or __context__)
        cause = getattr(error, "__cause__", None)
        if cause is not None and cls.classify_status(cause, seen=child_seen, depth=depth + 1):
            return True
        context = getattr(error, "__context__", None)
        if context is not None and cls.classify_status(context, seen=child_seen, depth=depth + 1):
            return True

        return False

    @classmethod
    def classify_payload(cls, payload_or_error: Any) -> bool:
        """Tier 2: Inspect JSON payload body for Canopy markers:
        - canopy_proxy == 'CANOPY_AI_CREDITS_PROXY'
        - reason == 'personal_wallet_empty'
        - code == 'no_billing_source'
        """
        payload_dict = cls._extract_payload_dict(payload_or_error)
        if not payload_dict:
            return False

        candidates = [payload_dict]
        if isinstance(payload_dict.get("error"), dict):
            candidates.append(payload_dict["error"])

        for candidate in candidates:
            canopy_proxy = str(candidate.get("canopy_proxy", "")).strip().upper()
            reason = str(candidate.get("reason", "")).strip().lower()
            code = str(candidate.get("code", "")).strip().lower()

            if (
                canopy_proxy == CANOPY_PROXY_IDENTIFIER
                or reason == CANOPY_REASON_WALLET_EMPTY
                or code == CANOPY_CODE_NO_BILLING
            ):
                return True

        return False

    @classmethod
    def classify_masked(
        cls,
        error: Any,
        seen: Optional[Set[int]] = None,
        depth: int = 0,
    ) -> bool:
        r"""Tier 3: Regex matching across APIConnectionError or string representations:
        - r"\b(?:status(?:_code)?|http|code|error|err)\s*[:=]?\s*402\b|\b402\s+(?:payment[_\s]required|payment|credits?|wallet|exhausted)\b"
        - r"\bpayment[_\s]required\b"
        - r"\bai credits are unavailable\b"
        - Canopy markers (personal_wallet_empty, canopy_ai_credits_proxy, no_billing_source)
        """
        if error is None or error == "" or isinstance(error, bool) or depth > 10:
            return False

        if seen is not None and id(error) in seen:
            return False

        if isinstance(error, (SyntaxError, IndentationError, TabError, NameError, TypeError, KeyError, ValueError)):
            if not any(hasattr(error, attr) for attr in ("status_code", "http_status", "code", "error_code", "status")):
                return False

        child_seen = set(seen) if seen is not None else set()
        child_seen.add(id(error))

        # If error is a dict, only check error fields — do not stringify prompt/content payloads
        if isinstance(error, dict):
            if any(k in error for k in REQUEST_PAYLOAD_KEYS):
                return False
            for key in ("error", "message", "detail", "reason", "description"):
                val = error.get(key)
                if isinstance(val, (str, dict)) and cls.classify_masked(val, seen=child_seen, depth=depth + 1):
                    return True
            return False

        text = str(error)
        msg = getattr(error, "message", None)
        if isinstance(msg, str) and msg:
            text = f"{text} {msg}"

        # Match payment required, credits unavailable, or canopy markers
        if (
            MASKED_PAYMENT_REQUIRED_PATTERN.search(text)
            or MASKED_CREDITS_UNAVAILABLE_PATTERN.search(text)
            or MASKED_CANOPY_MARKERS_PATTERN.search(text)
        ):
            return True

        # Match 402 with benign number guards
        if MASKED_402_PATTERN.search(text):
            for benign_pat in BENIGN_NUMBER_PATTERNS:
                if benign_pat.search(text):
                    return False
            return True

        # Check nested exception arguments if present
        args = getattr(error, "args", ())
        if len(args) > 1:
            for arg in args:
                if arg is not error and cls.classify_masked(arg, seen=child_seen, depth=depth + 1):
                    return True

        # Check chained exception causes (__cause__ or __context__)
        cause = getattr(error, "__cause__", None)
        if cause is not None and cls.classify_masked(cause, seen=child_seen, depth=depth + 1):
            return True
        context = getattr(error, "__context__", None)
        if context is not None and cls.classify_masked(context, seen=child_seen, depth=depth + 1):
            return True

        return False

    @classmethod
    def extract_correlation_id(cls, error: Any) -> Optional[str]:
        """Extract Canopy correlation ID from headers, payload body, or error text."""
        if error is None or error == "":
            return None

        header_names = (
            "x-canopy-correlation-id",
            "canopy-correlation-id",
            "x-canopy-request-id",
            "canopy-request-id",
            "x-correlation-id",
            "correlation-id",
            "x-request-id",
            "request-id",
        )

        # 1. Direct headers attribute on error or response
        for container in (error, getattr(error, "response", None)):
            if container is not None:
                headers = getattr(container, "headers", None)
                if isinstance(headers, dict) or hasattr(headers, "get"):
                    for h_name in header_names:
                        val = headers.get(h_name)
                        if val and isinstance(val, str) and val.strip():
                            return val.strip()
                        # Case-insensitive header dictionary lookup fallback
                        if isinstance(headers, dict):
                            for k, v in headers.items():
                                if isinstance(k, str) and k.lower() == h_name and v and isinstance(v, str) and v.strip():
                                    return v.strip()

        # 2. Dictionary / payload structure
        payload = cls._extract_payload_dict(error)
        if payload:
            candidates = [payload]
            if isinstance(payload.get("error"), dict):
                candidates.append(payload["error"])
            for c in candidates:
                for key in (
                    "canopy_correlation_id",
                    "correlation_id",
                    "request_id",
                    "x_canopy_request_id",
                    "x-canopy-request-id",
                ):
                    val = c.get(key)
                    if val and isinstance(val, str) and val.strip():
                        return val.strip()

        # 3. String / message regex
        text = str(error)
        msg = getattr(error, "message", None)
        if isinstance(msg, str):
            text = f"{text} {msg}"

        m = CORRELATION_ID_JSON_PATTERN.search(text)
        if m:
            return m.group(1).strip()

        m = CORRELATION_ID_HEADER_PATTERN.search(text)
        if m:
            return m.group(1).strip()

        return None

    @classmethod
    def extract_exhaustion_reason(cls, error: Any) -> Optional[str]:
        """Extract Canopy exhaustion reason token (e.g. 'personal_wallet_empty', 'no_billing_source')."""
        if error is None or error == "":
            return None

        # 1. Check payload structure
        payload = cls._extract_payload_dict(error)
        if payload:
            candidates = [payload]
            if isinstance(payload.get("error"), dict):
                candidates.append(payload["error"])
            for c in candidates:
                reason = c.get("reason")
                if reason and isinstance(reason, str) and reason.strip():
                    return reason.strip()
                code = c.get("code")
                if code and isinstance(code, str) and code.strip():
                    code_clean = code.strip()
                    if code_clean.lower() == CANOPY_CODE_NO_BILLING:
                        return code_clean

        # 2. Check string representation
        text = str(error).lower()
        if CANOPY_REASON_WALLET_EMPTY in text:
            return CANOPY_REASON_WALLET_EMPTY
        if CANOPY_CODE_NO_BILLING in text:
            return CANOPY_CODE_NO_BILLING
        if "canopy_ai_credits_proxy" in text:
            return "canopy_ai_credits_proxy"

        return None

    @classmethod
    def classify(
        cls,
        error: Any,
        seen: Optional[Set[int]] = None,
        depth: int = 0,
    ) -> CanopyClassificationResult:
        """Classify error across all 3 tiers and extract diagnostic details."""
        if error is None or error == "" or isinstance(error, bool) or depth > 10:
            return CanopyClassificationResult(is_exhausted=False)

        if seen is not None and id(error) in seen:
            return CanopyClassificationResult(is_exhausted=False)

        corr_id = cls.extract_correlation_id(error)
        payload = cls._extract_payload_dict(error) or {}
        error_sub = payload.get("error", {}) if isinstance(payload.get("error"), dict) else payload

        details: Dict[str, Any] = {}
        if corr_id:
            details["canopy_correlation_id"] = corr_id
        if "reason" in error_sub:
            details["reason"] = error_sub["reason"]
        if "code" in error_sub:
            details["code"] = error_sub["code"]
        if "canopy_proxy" in error_sub:
            details["canopy_proxy"] = error_sub["canopy_proxy"]
        if "message" in error_sub:
            details["message"] = error_sub["message"]

        # Tier 1: Status layer
        if cls.classify_status(error, seen=seen, depth=depth):
            details["tier"] = "status"
            return CanopyClassificationResult(
                is_exhausted=True,
                matched_tier="status",
                correlation_id=corr_id,
                details=details,
            )

        # Tier 2: Payload layer
        if cls.classify_payload(error):
            details["tier"] = "payload"
            return CanopyClassificationResult(
                is_exhausted=True,
                matched_tier="payload",
                correlation_id=corr_id,
                details=details,
            )

        # Tier 3: Masked layer
        if cls.classify_masked(error, seen=seen, depth=depth):
            details["tier"] = "masked"
            return CanopyClassificationResult(
                is_exhausted=True,
                matched_tier="masked",
                correlation_id=corr_id,
                details=details,
            )

        # Chained exception cause/context traversal
        child_seen = set(seen) if seen is not None else set()
        child_seen.add(id(error))

        cause = getattr(error, "__cause__", None)
        if cause is not None:
            res = cls.classify(cause, seen=child_seen, depth=depth + 1)
            if res.is_exhausted:
                if not corr_id and res.correlation_id:
                    return res
                return res

        context = getattr(error, "__context__", None)
        if context is not None:
            res = cls.classify(context, seen=child_seen, depth=depth + 1)
            if res.is_exhausted:
                return res

        return CanopyClassificationResult(
            is_exhausted=False,
            matched_tier=None,
            correlation_id=corr_id,
            details=details,
        )

    @classmethod
    def is_canopy_exhaustion(cls, error: Any) -> bool:
        """Return True if error matches any Canopy 402 exhaustion tier."""
        return cls.classify(error).is_exhausted

    # ------------------------------------------------------------------ #
    #  Internal Helpers                                                  #
    # ------------------------------------------------------------------ #

    @classmethod
    def _extract_payload_dict(cls, obj: Any) -> Optional[Dict[str, Any]]:
        """Extract a dictionary representation from an error, response, or string."""
        if isinstance(obj, dict):
            return obj

        response = getattr(obj, "response", None)
        if response is not None:
            if callable(getattr(response, "json", None)):
                try:
                    res = response.json()
                    if isinstance(res, dict):
                        return res
                except Exception:
                    pass
            text = getattr(response, "text", None)
            if isinstance(text, str):
                parsed = cls._parse_json_safe(text)
                if parsed is not None:
                    return parsed

        for attr in ("text", "body", "detail"):
            val = getattr(obj, attr, None)
            if isinstance(val, dict):
                return val
            if isinstance(val, str):
                parsed = cls._parse_json_safe(val)
                if parsed is not None:
                    return parsed

        if isinstance(obj, (str, bytes)):
            text = obj.decode("utf-8", "replace") if isinstance(obj, bytes) else obj
            parsed = cls._parse_json_safe(text)
            if parsed is not None:
                return parsed
            embedded = cls._extract_embedded_json(text)
            if embedded is not None:
                return embedded

        if isinstance(obj, Exception):
            msg = str(obj)
            embedded = cls._extract_embedded_json(msg)
            if embedded is not None:
                return embedded

        return None

    @staticmethod
    def _parse_json_safe(text: str) -> Optional[Dict[str, Any]]:
        stripped = text.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or (
            stripped.startswith("[") and stripped.endswith("]")
        ):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return None

    @staticmethod
    def _extract_embedded_json(text: str) -> Optional[Dict[str, Any]]:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        return None


# Module-level convenience functions
def is_canopy_exhaustion(error: Any) -> bool:
    """Convenience helper to check if an error represents Canopy credit exhaustion."""
    return CanopyClassifier.is_canopy_exhaustion(error)


def extract_canopy_correlation_id(error: Any) -> Optional[str]:
    """Convenience helper to extract Canopy correlation ID."""
    return CanopyClassifier.extract_correlation_id(error)


def extract_canopy_reason(error: Any) -> Optional[str]:
    """Convenience helper to extract Canopy exhaustion reason."""
    return CanopyClassifier.extract_exhaustion_reason(error)
