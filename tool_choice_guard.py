from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Set

import litellm
from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger

# Ensure Anthropic /v1/messages requests route through Chat Completions adapter
litellm.use_chat_completions_url_for_anthropic_messages = True


# ---------------------------------------------------------------------- #
#  Antigravity Failover Circuit Breaker & Exhaustion Detection           #
# ---------------------------------------------------------------------- #

STATE_PRIMARY = "primary"
STATE_FAILOVER = "failover"
STATE_PROBING = "probing"

EXHAUSTION_PHRASES = (
    "resource_exhausted",
    "your ai credits balance is too low to continue",
    "resource has been exhausted",
    "quota exceeded",
    "exceeded your current quota",
    "insufficient_quota",
)

GATEWAY_FAILOVER_PHRASES = (
    "502 bad gateway",
    "504 gateway timeout",
    "bad gateway",
    "gateway timeout",
    "gateway time-out",
    "bad_gateway",
    "gateway_timeout",
)

FAILOVER_PHRASES = (*EXHAUSTION_PHRASES, *GATEWAY_FAILOVER_PHRASES)

FAILOVER_STATUS_CODES = (429, 502, 504, "429", "502", "504")


def is_failover_error(error: Any) -> bool:
    """Return True if error matches known quota exhaustion (429) or upstream gateway failure (502, 504)."""
    if error is None or error == "":
        return False

    # Check status code / code attributes on exception or object
    status_code = getattr(error, "status_code", None)
    if status_code in FAILOVER_STATUS_CODES:
        return True
    http_status = getattr(error, "http_status", None)
    if http_status in FAILOVER_STATUS_CODES:
        return True
    code = getattr(error, "code", None)
    if code in FAILOVER_STATUS_CODES:
        return True

    # Check response attribute if present (e.g. httpx.HTTPStatusError or openai.APIStatusError)
    response = getattr(error, "response", None)
    if response is not None:
        resp_status = getattr(response, "status_code", None) or getattr(response, "status", None)
        if resp_status in FAILOVER_STATUS_CODES:
            return True

    # If it is a standard programming syntax/type exception without explicit status code, reject
    if isinstance(error, (SyntaxError, IndentationError, TabError, NameError, TypeError, KeyError)):
        return False

    # Check dict structure (only inspect error fields, never prompts or messages)
    if isinstance(error, dict):
        if (
            error.get("status_code") in FAILOVER_STATUS_CODES
            or error.get("error_code") in FAILOVER_STATUS_CODES
            or error.get("code") in FAILOVER_STATUS_CODES
        ):
            return True
        status_val = str(error.get("status", "")).strip().lower()
        if status_val in ("resource_exhausted", "429", "502", "504", "bad_gateway", "gateway_timeout"):
            return True
        type_val = str(error.get("type", "")).strip().lower()
        if type_val in ("bad_gateway", "gateway_timeout"):
            return True
        err_sub = error.get("error")
        if isinstance(err_sub, dict):
            if is_failover_error(err_sub):
                return True
        elif isinstance(err_sub, str):
            if is_failover_error(err_sub):
                return True
        msg = error.get("message")
        if isinstance(msg, str) and is_failover_error(msg):
            return True
        return False

    # Check string representation
    text = str(error)

    # Check if string is serialized JSON
    stripped = text.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and is_failover_error(parsed):
                return True
        except Exception:
            pass

    text_lower = text.lower()

    # Check known failover phrases (exhaustion + gateway errors)
    for phrase in FAILOVER_PHRASES:
        if phrase in text_lower:
            return True

    # Check structured 429, 502, 504 status/error mentions (avoid bare token counts, line numbers, port numbers)
    if re.search(r"\b(?:status(?:_code)?|http|code|error|err|rate[_\s]limit)\s*[:=]?\s*(?:429|502|504)\b", text_lower):
        return True
    if re.search(r"\b429\s+(?:too many requests|rate limit|quota|exhausted)", text_lower):
        return True
    if re.search(r"\b502\s+(?:bad gateway|gateway error|gateway|upstream|server error)", text_lower):
        return True
    if re.search(r"\b504\s+(?:gateway timeout|gateway time-out|timed? out|gateway)", text_lower):
        return True
    if "too many requests" in text_lower and ("rate" in text_lower or "limit" in text_lower or "429" in text_lower):
        return True

    # Check args or response attributes if present
    args = getattr(error, "args", ())
    for arg in args:
        if isinstance(arg, dict) and is_failover_error(arg):
            return True
        elif isinstance(arg, str) and is_failover_error(arg):
            return True

    return False


def is_exhaustion_error(error: Any) -> bool:
    """Return True if error matches known quota exhaustion or upstream gateway failure signatures."""
    return is_failover_error(error)


class AntigravityCircuitBreaker:
    """Circuit breaker for Antigravity upstream endpoints.

    States:
    - STATE_PRIMARY ("primary"): Standard routing to local/primary Antigravity endpoint.
    - STATE_FAILOVER ("failover"): Upstream quota/credit exhaustion detected; reroute to remote fallback.
    - STATE_PROBING ("probing"): Cooldown (default 15 mins / 900s) expired; probe primary with base target.
    """

    def __init__(
        self,
        cooldown_seconds: float = 900.0,
        time_fn: Optional[Callable[[], float]] = None,
        state_file: Optional[Path | str] = None,
    ) -> None:
        self.cooldown_seconds = float(cooldown_seconds)
        self._time_fn = time_fn if time_fn is not None else time.time
        self.state_file: Optional[Path] = Path(state_file) if state_file is not None else None
        self._state = STATE_PRIMARY
        self._last_failure_time: Optional[float] = None
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_file:
            return
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self._state = data.get("state", STATE_PRIMARY)
                self._last_failure_time = data.get("last_failure_time")
        except Exception:
            pass

    def _save_state(self, reason: Optional[Any] = None) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "state": self._state,
                "last_failure_time": self._last_failure_time,
                "reason": str(reason) if reason else None,
                "updated_at": self._time_fn(),
            }
            tmp_path = self.state_file.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp_path.replace(self.state_file)
        except Exception:
            pass

    @property
    def state(self) -> str:
        self._load_state()
        if self._state == STATE_FAILOVER and self._last_failure_time is not None:
            if self._time_fn() - self._last_failure_time >= self.cooldown_seconds:
                self._state = STATE_PROBING
                self._save_state("cooldown_elapsed")
        return self._state

    def trigger_failover(self, error: Any = None) -> None:
        self._state = STATE_FAILOVER
        self._last_failure_time = self._time_fn()
        self._save_state(error)

    def record_failure(self, error: Any = None) -> bool:
        if error is not None and not is_failover_error(error):
            return False

        self._state = STATE_FAILOVER
        self._last_failure_time = self._time_fn()
        self._save_state(error)
        return True

    def record_success(self, model: Optional[str] = None) -> bool:
        # Ignore success on remote fallback models so they don't prematurely close probing
        if model and "antigravity-remote" in model:
            return False

        if self.state == STATE_PROBING:
            self._state = STATE_PRIMARY
            self._last_failure_time = None
            self._save_state("probe_success")
            return True
        return False

    def reset(self) -> None:
        self._state = STATE_PRIMARY
        self._last_failure_time = None
        self._save_state("manual_reset")

    @staticmethod
    def _to_remote_model(model: str) -> str:
        if model.startswith("antigravity-remote-"):
            return model
        if model.startswith("antigravity-"):
            return "antigravity-remote-" + model[len("antigravity-"):]
        return model

    @staticmethod
    def _to_base_model(model: str) -> str:
        return model

    def get_target_for_model(self, model: str) -> str:
        current_state = self.state
        if current_state == STATE_FAILOVER:
            return self._to_remote_model(model)
        elif current_state == STATE_PROBING:
            return self._to_base_model(model)
        else:  # STATE_PRIMARY
            return self._to_base_model(model)


class StripOrphanToolChoiceGuard(CustomLogger):
    """Drop tool-only params when a request doesn't actually include tools.

    Also sanitize Responses API ``input`` arrays to remove
    ``function_call_output`` items that have a missing/null ``call_id``
    or whose ``call_id`` doesn't reference a preceding ``function_call``
    in the same input array.  This prevents Azure OpenAI from returning
    HTTP 400 ("Invalid Value: 'input.call_id'. Function call output
    requires call_id.") when Codex Desktop's auto-compact truncation
    removes earlier ``function_call`` turns while leaving their outputs.
    """

    def __init__(self, circuit_breaker: Optional[AntigravityCircuitBreaker] = None):
        super().__init__()
        self.circuit_breaker = circuit_breaker or AntigravityCircuitBreaker()

    # ------------------------------------------------------------------ #
    #  tool_choice guard (existing)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _has_tools(tools: Any) -> bool:
        if tools is None:
            return False
        if isinstance(tools, list):
            return len(tools) > 0
        return bool(tools)

    # ------------------------------------------------------------------ #
    #  Responses API input sanitizer (NEW)
    # ------------------------------------------------------------------ #

    CALL_ITEM_TYPES: Set[str] = {
        "function_call",
        "custom_tool_call",
        "google_search_call",
        "mcp_server_tool_call",
        "server_tool_use",
    }

    RESULT_ITEM_TYPES: Set[str] = {
        "function_call_output",
        "custom_tool_call_output",
        "google_search_result",
        "mcp_server_tool_result",
        "file_search_result",
    }

    @classmethod
    def _is_call_item(cls, item_type: Any) -> bool:
        if not isinstance(item_type, str):
            return False
        return item_type in cls.CALL_ITEM_TYPES or item_type.endswith("_call")

    @classmethod
    def _is_result_item(cls, item_type: Any) -> bool:
        if not isinstance(item_type, str):
            return False
        return (
            item_type in cls.RESULT_ITEM_TYPES
            or item_type.endswith("_result")
            or item_type.endswith("_call_output")
        )

    @staticmethod
    def _sanitize_responses_input(input_items: List[Any]) -> List[Any]:
        """Return a cleaned copy of the Responses API ``input`` list.

        Rules:
        1. Every result-like tool output item MUST have a non-empty
           reference id (``call_id`` or ``tool_use_id``).
        2. Every result-like tool output reference id MUST point to a
           tool-call item that appears *earlier* in the same list.

        Items that violate either rule are silently dropped and logged.
        """
        # First pass: collect all valid call ids from tool call items
        valid_call_ids: Set[str] = set()
        for item in input_items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if StripOrphanToolChoiceGuard._is_call_item(item_type):
                call_id = item.get("call_id") or item.get("id") or item.get("tool_use_id")
                if call_id:
                    valid_call_ids.add(str(call_id))

        # Second pass: filter out broken result items referencing missing calls
        sanitized: List[Any] = []
        dropped_count = 0
        for item in input_items:
            if not isinstance(item, dict):
                sanitized.append(item)
                continue

            item_type = item.get("type")

            if StripOrphanToolChoiceGuard._is_result_item(item_type):
                call_id = item.get("call_id") or item.get("tool_use_id")
                # Rule 1: reference id must be present and non-empty
                if not call_id:
                    dropped_count += 1
                    verbose_proxy_logger.warning(
                        "StripOrphanToolChoiceGuard: dropped %s with missing/null call reference",
                        item_type,
                    )
                    continue
                # Rule 2: reference id must point to a known preceding tool call
                call_id_str = str(call_id)
                if call_id_str not in valid_call_ids:
                    dropped_count += 1
                    verbose_proxy_logger.warning(
                        "StripOrphanToolChoiceGuard: dropped orphaned "
                        "%s with call_id=%s (no matching tool call in input)",
                        item_type,
                        call_id_str,
                    )
                    continue

            sanitized.append(item)

        if dropped_count > 0:
            verbose_proxy_logger.info(
                "StripOrphanToolChoiceGuard: sanitized Responses API input, "
                "dropped %d orphaned/invalid function_call_output item(s)",
                dropped_count,
            )

        return sanitized

    # ------------------------------------------------------------------ #
    #  Antigravity dynamic routing & reasoning effort translation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _detect_reasoning_effort(data: Optional[dict]) -> str:
        if not isinstance(data, dict):
            return "default"

        thinking = data.get("thinking")
        if isinstance(thinking, dict):
            t_type = thinking.get("type")
            if t_type == "adaptive":
                return "high"
            elif t_type == "disabled":
                return "default"
            elif t_type == "enabled" or "budget_tokens" in thinking:
                budget = thinking.get("budget_tokens")
                if budget is not None:
                    try:
                        budget_int = int(budget)
                        if budget_int <= 2048:
                            return "low"
                        elif budget_int <= 10000:
                            return "medium"
                        else:
                            return "high"
                    except (ValueError, TypeError):
                        pass

        effort = data.get("reasoning_effort")
        reasoning_dict = data.get("reasoning")
        if isinstance(reasoning_dict, dict):
            effort = effort or reasoning_dict.get("effort")
        if isinstance(effort, str):
            effort = effort.lower().strip()
            if effort in ("low", "minimal"):
                return "low"
            elif effort == "medium":
                return "medium"
            elif effort in ("high", "xhigh", "max"):
                return "high"

        return "default"

    @staticmethod
    def _resolve_antigravity_target(requested_model: str, effort: str) -> str:
        raw = (requested_model or "").lower().strip()
        clean = raw.split("/")[-1]
        if "opus" in clean or "mythos" in clean:
            return "antigravity-opus-4.6"
        if "haiku" in clean:
            resolved_effort = "low" if effort == "default" else effort
            return f"antigravity-flash-{resolved_effort}"
        # Default family: Sonnet, claude-gpt-5, gpt-5.4, gpt-*, wildcard
        resolved_effort = "high" if effort == "default" else effort
        return f"antigravity-flash-{resolved_effort}"

    # ------------------------------------------------------------------ #
    #  main hook
    # ------------------------------------------------------------------ #

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: Optional[dict],
        call_type: str,
    ) -> Optional[dict]:
        if not isinstance(data, dict):
            return data

        # --- Dynamic Antigravity Model & Reasoning Effort Resolution ---
        current_model = data.get("model", "")
        if isinstance(current_model, str):
            clean_check = current_model.lower().strip()
            if (
                any(clean_check.startswith(prefix) for prefix in ("claude-", "gpt-", "azure/"))
                or clean_check in ("*", "claude-gpt-5")
            ):
                effort = self._detect_reasoning_effort(data)
                target_model = self._resolve_antigravity_target(current_model, effort)
                resolved_effort = (
                    "low"
                    if "low" in target_model
                    else (
                        "medium"
                        if "medium" in target_model
                        else (
                            "high"
                            if "high" in target_model
                            else ("high" if effort == "default" else effort)
                        )
                    )
                )
                data["reasoning_effort"] = resolved_effort
                data["model"] = self.circuit_breaker.get_target_for_model(target_model)
                verbose_proxy_logger.info(
                    "StripOrphanToolChoiceGuard: routed %s (effort=%s) -> %s (reasoning_effort=%s, cb_state=%s)",
                    current_model,
                    effort,
                    data["model"],
                    resolved_effort,
                    self.circuit_breaker.state,
                )
            elif clean_check.startswith("antigravity-") and not clean_check.startswith("antigravity-remote-"):
                data["model"] = self.circuit_breaker.get_target_for_model(current_model)

        # --- Universal parameter sanitizers for Azure OpenAI ---
        # Azure OpenAI rejects 'call_type' and 'route_type' with HTTP 400
        for bad_param in ("call_type", "route_type"):
            if bad_param in data:
                data.pop(bad_param, None)
                verbose_proxy_logger.debug(
                    "StripOrphanToolChoiceGuard removed unsupported '%s' (call_type=%s, model=%s)",
                    bad_param,
                    call_type,
                    data.get("model"),
                )

        # --- Responses API input sanitizer ---
        # For /v1/responses calls, data contains an "input" field (list of items)
        if call_type in ("aresponses", "_aresponses_websocket", "responses"):
            raw_input = data.get("input")
            if isinstance(raw_input, list) and raw_input:
                # Only run sanitizer if there are result-like items carrying
                # tool outputs or server-tool results.
                has_result_items = any(
                    isinstance(item, dict)
                    and self._is_result_item(item.get("type"))
                    for item in raw_input
                )
                if has_result_items:
                    data["input"] = self._sanitize_responses_input(raw_input)

        # --- Responses API unsupported parameter sanitizer ---
        # Azure Responses API rejects 'stop_sequences' with HTTP 400
        if "stop_sequences" in data:
            data.pop("stop_sequences", None)
            verbose_proxy_logger.debug(
                "StripOrphanToolChoiceGuard removed unsupported 'stop_sequences' (call_type=%s, model=%s)",
                call_type,
                data.get("model"),
            )

        # --- tool_choice guard across all endpoints ---
        if "tool_choice" in data or "parallel_tool_calls" in data:
            tools = data.get("tools")
            if not self._has_tools(tools):
                stripped = False
                for key in ("tool_choice", "parallel_tool_calls"):
                    if key in data:
                        data.pop(key, None)
                        stripped = True
                if stripped:
                    verbose_proxy_logger.debug(
                        "StripOrphanToolChoiceGuard removed tool params without tools "
                        "(call_type=%s, model=%s)",
                        call_type,
                        data.get("model"),
                    )

        return data

    async def async_pre_call_deployment_hook(
        self, kwargs: dict[str, Any], call_type: Any = None
    ) -> Optional[dict]:
        """Sanitize deployment kwargs immediately before dispatching to provider."""
        if not isinstance(kwargs, dict):
            return kwargs

        # Strip call_type and route_type if present
        for bad_param in ("call_type", "route_type"):
            if bad_param in kwargs:
                kwargs.pop(bad_param, None)

        # Strip orphan tool_choice / parallel_tool_calls if tools is empty or None
        # (Crucial when Anthropic web_search tools are extracted into web_search_options,
        # leaving tools=None while tool_choice remains set to 'auto')
        if "tool_choice" in kwargs or "parallel_tool_calls" in kwargs:
            tools = kwargs.get("tools")
            if not self._has_tools(tools):
                kwargs.pop("tool_choice", None)
                kwargs.pop("parallel_tool_calls", None)
                verbose_proxy_logger.debug(
                    "StripOrphanToolChoiceGuard [deployment_hook]: removed tool_choice without tools"
                )

        return kwargs

    async def async_post_call_failure_hook(
        self,
        request_data: dict,
        original_exception: Exception,
        user_api_key_dict: Any,
        traceback_str: Optional[str] = None,
        *args: Any,
        **kwargs: Any,
    ) -> Optional[Any]:
        if original_exception is None:
            return None

        # Ignore failures from remote fallback models
        model = (request_data.get("model") or "") if isinstance(request_data, dict) else ""
        if "antigravity-remote" in model:
            verbose_proxy_logger.debug(
                "StripOrphanToolChoiceGuard: Ignoring failure on remote model %s", model
            )
            return None

        if is_failover_error(original_exception):
            self.circuit_breaker.record_failure(original_exception)
            verbose_proxy_logger.warning(
                "StripOrphanToolChoiceGuard: Failover error detected (%s). Circuit breaker switched to %s",
                original_exception,
                self.circuit_breaker.state,
            )
        return None

    async def async_post_call_success_hook(
        self,
        data: dict,
        user_api_key_dict: Any,
        response: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        model = data.get("model") if isinstance(data, dict) else None
        self.circuit_breaker.record_success(model)
        return response


_default_state_file = Path.home() / ".litellm" / "failover_state.json"
circuit_breaker = AntigravityCircuitBreaker(state_file=_default_state_file)
proxy_guard = StripOrphanToolChoiceGuard(circuit_breaker=circuit_breaker)


