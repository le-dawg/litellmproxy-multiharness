"""Diagnostic Error Enricher for LiteLLM MultiHarness Endpoint Abstraction.

Produces structured HTTP 402 error payloads matching Interface Contract 3
for prepaid credit exhaustion and fast-fail backpressure.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any, Dict, Optional

logger = logging.getLogger("litellm.endpoint_abstraction.error_enricher")

DEFAULT_DEPLETED_MESSAGE = (
    "[Unstoppable AI Resource Depleted] All configured prepaid wallets have zero credit. "
    "Processing is paused while the background probe monitors fund replenishment."
)
DEFAULT_RESUME_INSTRUCTION = (
    "Run 'claude --resume' once funds are replenished or after receiving the desktop notification."
)
DEFAULT_STATUS = "paused"
DEFAULT_ERROR_TYPE = "payment_required"
DEFAULT_ERROR_CODE = 402
CORRELATION_ID_PREFIX = "req_canopy_"
CORRELATION_ID_REGEX = re.compile(r"^req_canopy_[0-9A-Fa-fA-Za-z_]+$")

# Upstream and proxy header candidates for correlation ID resolution
CORRELATION_HEADER_NAMES = (
    "x-canopy-correlation-id",
    "canopy-correlation-id",
    "x-canopy-request-id",
    "canopy-request-id",
    "x-correlation-id",
    "correlation-id",
    "x-request-id",
    "request-id",
    "canopy_correlation_id",
)

try:
    from fastapi import HTTPException
except ImportError:
    class HTTPException(Exception):  # type: ignore[no-redef]
        """Fallback HTTPException when FastAPI is not available."""

        def __init__(
            self,
            status_code: int = DEFAULT_ERROR_CODE,
            detail: Any = None,
            headers: Optional[Dict[str, str]] = None,
        ) -> None:
            super().__init__(str(detail))
            self.status_code = status_code
            self.detail = detail
            self.headers = headers or {}


class ErrorDetailDict(dict):
    """Dictionary supporting both flat keys and nested 'error' key lookup."""

    def __getitem__(self, key: Any) -> Any:
        if key == "error":
            return self
        return super().__getitem__(key)

    def __contains__(self, key: Any) -> bool:
        if key == "error":
            return True
        return super().__contains__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        if key == "error":
            return self
        return super().get(key, default)


class CanopyResourceDepletedError(HTTPException):
    """Specialized HTTP 402 exception for Unstoppable AI Resource Depletion.

    Subclasses fastapi.HTTPException for native FastAPI response serialization
    while providing direct programmatic access to structured payload and metadata.
    """

    def __init__(
        self,
        payload: Dict[str, Any],
        correlation_id: str,
        status_code: int = DEFAULT_ERROR_CODE,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.payload = payload
        self.correlation_id = correlation_id
        raw_body = payload.get("error", payload)
        error_body = ErrorDetailDict(raw_body) if isinstance(raw_body, dict) else raw_body

        response_headers = dict(headers or {})
        response_headers.setdefault("X-Canopy-Correlation-Id", correlation_id)
        response_headers.setdefault("Content-Type", "application/json")

        super().__init__(
            status_code=status_code,
            detail=error_body,
            headers=response_headers,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the structured Interface Contract 3 payload dictionary."""
        return self.payload

    def to_json(self, indent: Optional[int] = None) -> str:
        """Return JSON serialized representation."""
        return json.dumps(self.payload, indent=indent)

    def __repr__(self) -> str:
        return (
            f"CanopyResourceDepletedError(status_code={self.status_code}, "
            f"correlation_id={self.correlation_id!r})"
        )


class ErrorEnricher:
    """Utility class synthesizing enriched HTTP 402 diagnostic responses."""

    @staticmethod
    def generate_fallback_correlation_id(
        prefix: str = CORRELATION_ID_PREFIX,
        hex_length: int = 8,
    ) -> str:
        """Generate high-entropy fallback correlation ID matching 'req_canopy_01ABCDEF'.

        Uses secrets.token_hex for cryptographic randomness formatted uppercase.
        """
        token = secrets.token_hex(hex_length // 2).upper()
        return f"{prefix}{token}"

    @classmethod
    def extract_correlation_id(
        cls,
        source: Any = None,
        headers: Optional[Dict[str, Any]] = None,
        fallback: bool = True,
    ) -> Optional[str]:
        """Resolve correlation ID across multiple sources with strict precedence.

        Precedence:
        1. Explicit string passed in source (if already a valid ID or starting with 'req_')
        2. Extracted from headers dict (case-insensitive lookup)
        3. Extracted from source (dict, request, response, Exception)
        4. Extracted via CanopyClassifier.extract_correlation_id if source is an exception
        5. Generated fallback 'req_canopy_<hex>' if fallback=True.
        """
        # 1. Direct non-empty string source
        if isinstance(source, str) and source.strip():
            stripped = source.strip()
            # If string is a JSON payload, parse it
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    parsed = json.loads(stripped)
                    cid = cls.extract_correlation_id(source=parsed, fallback=False)
                    if cid:
                        return cid
                except Exception:
                    pass
            else:
                return stripped

        # 2. Inspect headers dictionary
        if headers and (isinstance(headers, dict) or hasattr(headers, "get")):
            for candidate in CORRELATION_HEADER_NAMES:
                val = headers.get(candidate)
                if val and isinstance(val, str) and val.strip():
                    return val.strip()
                # Case-insensitive dictionary fallback
                if isinstance(headers, dict):
                    for k, v in headers.items():
                        if (
                            isinstance(k, str)
                            and k.lower() == candidate
                            and v
                            and isinstance(v, str)
                            and v.strip()
                        ):
                            return v.strip()

        # 3. Inspect dictionary / request data / pause info source
        if isinstance(source, dict):
            # Check nested headers
            for h_key in ("extra_headers", "headers"):
                nested_h = source.get(h_key)
                if isinstance(nested_h, dict):
                    cid = cls.extract_correlation_id(headers=nested_h, fallback=False)
                    if cid:
                        return cid

            # Check metadata dict
            metadata = source.get("metadata")
            if isinstance(metadata, dict):
                cid = cls.extract_correlation_id(source=metadata, fallback=False)
                if cid:
                    return cid

            # Check direct correlation ID keys
            for direct_key in (
                "canopy_correlation_id",
                "correlation_id",
                "request_id",
                "x_canopy_request_id",
                "x-canopy-request-id",
            ):
                val = source.get(direct_key)
                if val and isinstance(val, str) and val.strip():
                    return val.strip()

            # Check nested 'error' dict
            error_sub = source.get("error")
            if isinstance(error_sub, dict):
                cid = cls.extract_correlation_id(source=error_sub, fallback=False)
                if cid:
                    return cid

        # 4. Delegate to CanopyClassifier for error / response objects
        try:
            from service.agentic.endpoint_abstraction.canopy_classifier import (
                CanopyClassifier,
            )

            canopy_cid = CanopyClassifier.extract_correlation_id(source)
            if canopy_cid and isinstance(canopy_cid, str) and canopy_cid.strip():
                return canopy_cid.strip()
        except ImportError:
            pass

        # 5. Inspect headers on response/error attributes directly
        for attr in ("headers", "response"):
            obj = getattr(source, attr, None)
            if obj is not None:
                h = getattr(obj, "headers", None) if attr == "response" else obj
                if isinstance(h, dict) or hasattr(h, "get"):
                    cid = cls.extract_correlation_id(headers=h, fallback=False)
                    if cid:
                        return cid

        # 6. Fallback generation
        if fallback:
            return cls.generate_fallback_correlation_id()
        return None

    @classmethod
    def build_payload(
        cls,
        correlation_id: Optional[str] = None,
        message: Optional[str] = None,
        resume_instruction: Optional[str] = None,
        status: Optional[str] = None,
        error_type: Optional[str] = None,
        code: Optional[int] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Synthesize Interface Contract 3 structured error payload dictionary."""
        resolved_cid = correlation_id or cls.generate_fallback_correlation_id()

        error_dict: Dict[str, Any] = {
            "message": message or DEFAULT_DEPLETED_MESSAGE,
            "type": error_type or DEFAULT_ERROR_TYPE,
            "code": code if code is not None else DEFAULT_ERROR_CODE,
            "canopy_correlation_id": resolved_cid,
            "status": status or DEFAULT_STATUS,
            "resume_instruction": resume_instruction or DEFAULT_RESUME_INSTRUCTION,
        }

        if extra_fields and isinstance(extra_fields, dict):
            for k, v in extra_fields.items():
                if k not in error_dict:
                    error_dict[k] = v

        return {"error": error_dict}

    @classmethod
    def build_json(cls, indent: Optional[int] = None, **kwargs: Any) -> str:
        """Return JSON serialized representation of Interface Contract 3 payload."""
        return json.dumps(cls.build_payload(**kwargs), indent=indent)

    @classmethod
    def create_http_exception(
        cls,
        correlation_id: Optional[str] = None,
        source: Any = None,
        headers: Optional[Dict[str, Any]] = None,
        message: Optional[str] = None,
        resume_instruction: Optional[str] = None,
        status: Optional[str] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> CanopyResourceDepletedError:
        """Create a CanopyResourceDepletedError (fastapi.HTTPException subclass)."""
        cid = correlation_id or cls.extract_correlation_id(
            source=source, headers=headers
        )
        payload = cls.build_payload(
            correlation_id=cid,
            message=message,
            resume_instruction=resume_instruction,
            status=status,
            extra_fields=extra_fields,
        )
        return CanopyResourceDepletedError(
            payload=payload,
            correlation_id=cid,
            status_code=DEFAULT_ERROR_CODE,
            headers={"X-Canopy-Correlation-Id": cid},
        )

    @classmethod
    def format_http_exception(
        cls,
        correlation_id: Optional[str] = None,
        source: Any = None,
        headers: Optional[Dict[str, Any]] = None,
        message: Optional[str] = None,
        resume_instruction: Optional[str] = None,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> CanopyResourceDepletedError:
        """Convenience alias for create_http_exception matching hook invocations."""
        return cls.create_http_exception(
            correlation_id=correlation_id,
            source=source,
            headers=headers,
            message=message,
            resume_instruction=resume_instruction,
            status=status,
            extra_fields=extra_fields,
        )

    @classmethod
    def create_litellm_exception(
        cls,
        correlation_id: Optional[str] = None,
        source: Any = None,
        model: str = "deepseek-v4-pro",
        llm_provider: str = "unstoppable",
        **kwargs: Any,
    ) -> Any:
        """Synthesize litellm.exceptions.APIError carrying enriched 402 metadata."""
        cid = correlation_id or cls.extract_correlation_id(source=source)
        payload = cls.build_payload(correlation_id=cid, **kwargs)
        msg = payload["error"]["message"]

        try:
            import litellm.exceptions

            exc = litellm.exceptions.APIError(
                status_code=DEFAULT_ERROR_CODE,
                message=msg,
                llm_provider=llm_provider,
                model=model,
            )
            setattr(exc, "payload", payload)
            setattr(exc, "canopy_correlation_id", cid)
            return exc
        except (ImportError, AttributeError):
            return cls.create_http_exception(correlation_id=cid, **kwargs)

    @classmethod
    def format_pause_exception(
        cls,
        error: Any = None,
        request_data: Optional[Dict[str, Any]] = None,
        pause_info: Optional[Dict[str, Any]] = None,
    ) -> CanopyResourceDepletedError:
        """Primary integration helper for LiteLLM hooks upon all-wallets-exhausted."""
        cid = (
            cls.extract_correlation_id(source=error, fallback=False)
            or cls.extract_correlation_id(source=request_data, fallback=False)
            or (
                pause_info.get("correlation_id")
                if isinstance(pause_info, dict)
                else None
            )
            or cls.generate_fallback_correlation_id()
        )
        return cls.create_http_exception(correlation_id=cid, source=error)

    @classmethod
    def format_fast_fail(
        cls,
        request_data: Optional[Dict[str, Any]] = None,
        pause_info: Optional[Dict[str, Any]] = None,
    ) -> CanopyResourceDepletedError:
        """Primary integration helper for pre-call fast fail when pause lock active."""
        cid = (
            cls.extract_correlation_id(source=request_data, fallback=False)
            or (
                pause_info.get("correlation_id")
                if isinstance(pause_info, dict)
                else None
            )
            or cls.generate_fallback_correlation_id()
        )
        return cls.create_http_exception(correlation_id=cid, source=request_data)
