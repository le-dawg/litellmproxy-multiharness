from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Set

import litellm
from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger

# Ensure repository root, workspace, and ~/.litellm are accessible for modular imports
_candidate_roots = [
    Path(__file__).resolve().parent,
    Path.home() / "Desktop" / "dawgctor-personal-tools" / "litellmproxy-multiharness",
    Path.home() / ".litellm",
]
for _cr in _candidate_roots:
    if _cr.exists() and str(_cr) not in sys.path:
        sys.path.append(str(_cr))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from service.agentic.endpoint_abstraction.canopy_classifier import (
        BENIGN_NUMBER_PATTERNS,
        CanopyClassifier,
    )
except ImportError:
    CanopyClassifier = None
    BENIGN_NUMBER_PATTERNS = ()

try:
    from service.agentic.endpoint_abstraction.wallet_manager import WalletManager
except ImportError:
    WalletManager = None

try:
    from service.agentic.endpoint_abstraction.pause_manager import PauseManager
except ImportError:
    PauseManager = None

try:
    from service.agentic.endpoint_abstraction.error_enricher import (
        CanopyResourceDepletedError,
        ErrorEnricher,
    )
except ImportError:
    ErrorEnricher = None
    CanopyResourceDepletedError = None

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
    # Canopy Proxy AI Credit exhaustion markers
    "personal_wallet_empty",
    "no_billing_source",
    "payment required",
    "payment_required",
    "ai credits are unavailable",
    "canopy_ai_credits_proxy",
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

# ---------------------------------------------------------------------- #
#  Unstoppable Code Local Key Resolution                                 #
# ---------------------------------------------------------------------- #

UNSTOPPABLE_KEY_FILES = (
    Path("~/.unstoppable-code-anthropic-key").expanduser(),
    Path("~/.litellm/.unstoppable_key").expanduser(),
)


def get_unstoppable_code_key() -> Optional[str]:
    """Retrieve the active Unstoppable Code Anthropic API/auth key.

    Priority:
    1. Active healthy key from WalletManager (if available)
    2. Dedicated hidden files (~/.unstoppable-code-anthropic-key, ~/.litellm/.unstoppable_key)
    3. Environment variables (UNSTOPPABLE_CODE_KEY, UNSTOPPABLE_ANTHROPIC_KEY)
    4. Auto-decryption fallback from ~/.unstoppable-code-secure/canopy-cloud-account.enc via macOS Keychain
    """
    try:
        if "_wallet_manager" in globals() and _wallet_manager is not None:
            active_key = _wallet_manager.get_active_key()
            if active_key and active_key.startswith("csk_"):
                return active_key
    except Exception:
        pass

    import os

    # 1. Hidden config files
    for path in UNSTOPPABLE_KEY_FILES:
        try:
            if path.is_file():
                content = path.read_text().strip()
                if content and content.startswith("csk_"):
                    return content
        except Exception:
            pass

    # 2. Environment variables
    env_token = os.environ.get("UNSTOPPABLE_CODE_KEY") or os.environ.get("UNSTOPPABLE_ANTHROPIC_KEY")
    if env_token and env_token.strip():
        return env_token.strip()

    # 3. Dynamic Keychain auto-decrypt fallback
    enc_file = Path("~/.unstoppable-code-secure/canopy-cloud-account.enc").expanduser()
    if enc_file.is_file():
        try:
            import subprocess
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
            from cryptography.hazmat.primitives import hashes

            cmd = ["security", "find-generic-password", "-w", "-s", "Unstoppable Code Safe Storage"]
            pw = subprocess.check_output(cmd, timeout=3).strip()
            with open(enc_file, "rb") as f:
                enc_data = f.read()
            kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1003)
            key = kdf.derive(pw)
            cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
            dec = cipher.decryptor().update(enc_data[3:]) + cipher.decryptor().finalize()
            account = json.loads(dec[:-dec[-1]].decode("utf-8"))
            extracted = account.get("apiKey")
            if isinstance(extracted, str) and extracted.startswith("csk_"):
                try:
                    for p in UNSTOPPABLE_KEY_FILES:
                        p.write_text(extracted.strip() + "\n")
                        p.chmod(0o600)
                except Exception:
                    pass
                return extracted.strip()
        except Exception as e:
            verbose_proxy_logger.warning("get_unstoppable_code_key auto-decrypt failed: %s", e)

    return None


def is_unstoppable_opus_model(model_name: Optional[str]) -> bool:
    """Return True if request targets Unstoppable Opus 5.5 / DeepSeek V4 Pro."""
    clean = (model_name or "").lower().strip()
    return any(
        target in clean
        for target in (
            "opus-5.5",
            "opus-5-5",
            "opus 5.5",
            "opus[1m]",
            "deepseek-v4-pro",
            "deepseek/deepseek-v4-pro",
        )
    )


def is_unstoppable_sonnet_model(model_name: Optional[str]) -> bool:
    """Return True if request targets Unstoppable Sonnet 5.5 / GLM 5.3 Flash."""
    clean = (model_name or "").lower().strip()
    return any(
        target in clean
        for target in (
            "sonnet-5.5",
            "sonnet-5-5",
            "sonnet 5.5",
            "glm-5.3-flash",
            "glm-5.3",
        )
    )


def is_unstoppable_model(model_name: Optional[str], api_base: Optional[str] = None) -> bool:
    """Return True if request targets Unstoppable Code AI credits routing."""
    clean_base = (api_base or "").lower().strip()
    if "unstoppable.ai" in clean_base:
        return True
    return is_unstoppable_opus_model(model_name) or is_unstoppable_sonnet_model(model_name)


FAILOVER_PHRASES = (*EXHAUSTION_PHRASES, *GATEWAY_FAILOVER_PHRASES)

FAILOVER_STATUS_CODES = (402, 429, 502, 504, "402", "429", "502", "504")

# False-positive guardrails for tool_choice_guard (reject explanations and benign 402 numbers)
BENIGN_FAILOVER_PATTERNS = (
    *BENIGN_NUMBER_PATTERNS,
    re.compile(r"\b(?:explanation|asking\s+about|explaining)\b", re.IGNORECASE),
    re.compile(r"\bfound\s+\d+\s+results\b", re.IGNORECASE),
)


def is_failover_error(
    error: Any,
    seen: Optional[Set[int]] = None,
    depth: int = 0,
) -> bool:
    """Return True if error matches known quota exhaustion (402, 429) or upstream gateway failure (502, 504)."""
    if error is None or error == "" or isinstance(error, bool) or depth > 10:
        return False

    if seen is not None and id(error) in seen:
        return False

    # Tier 0: Direct CanopyClassifier delegation if available (only at root)
    if depth == 0 and CanopyClassifier is not None:
        try:
            if CanopyClassifier.is_canopy_exhaustion(error):
                return True
        except Exception:
            pass

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

    child_seen = set(seen) if seen is not None else set()
    child_seen.add(id(error))

    # Check dict structure (only inspect error fields, never prompts or messages)
    if isinstance(error, dict):
        if (
            error.get("status_code") in FAILOVER_STATUS_CODES
            or error.get("error_code") in FAILOVER_STATUS_CODES
            or error.get("code") in FAILOVER_STATUS_CODES
        ):
            return True
        status_val = str(error.get("status", "")).strip().lower()
        if status_val in (
            "resource_exhausted",
            "402",
            "429",
            "502",
            "504",
            "bad_gateway",
            "gateway_timeout",
            "payment_required",
        ):
            return True
        type_val = str(error.get("type", "")).strip().lower()
        if type_val in ("bad_gateway", "gateway_timeout", "payment_required"):
            return True
        reason_val = str(error.get("reason", "")).strip().lower()
        if reason_val in ("personal_wallet_empty", "no_billing_source"):
            return True
        err_sub = error.get("error")
        if isinstance(err_sub, (dict, str)):
            if is_failover_error(err_sub, seen=child_seen, depth=depth + 1):
                return True
        msg = error.get("message")
        if isinstance(msg, str) and is_failover_error(msg, seen=child_seen, depth=depth + 1):
            return True
        return False

    # Check string representation
    text = str(error)

    # Check if string is serialized JSON
    stripped = text.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and is_failover_error(parsed, seen=child_seen, depth=depth + 1):
                return True
        except Exception:
            pass

    text_lower = text.lower()

    # 1. Direct gateway failover phrases (502, 504)
    for phrase in GATEWAY_FAILOVER_PHRASES:
        if phrase in text_lower:
            return True
    if re.search(r"\b502\s+(?:bad gateway|gateway error|gateway|upstream|server error)", text_lower):
        return True
    if re.search(r"\b504\s+(?:gateway timeout|gateway time-out|timed? out|gateway)", text_lower):
        return True

    # 2. Direct rate-limit failovers (429)
    if re.search(r"\b429\s+(?:too many requests|rate limit|quota|exhausted)", text_lower):
        return True
    if "too many requests" in text_lower and ("rate" in text_lower or "limit" in text_lower or "429" in text_lower):
        return True
    if re.search(r"\b(?:status(?:_code)?|http|code|error|err|rate[_\s]limit)\s*[:=]?\s*(?:429|502|504)\b", text_lower):
        return True

    # 3. Reject benign probes (specifically guards against false 402 credit exhaustion matches)
    for benign_pat in BENIGN_FAILOVER_PATTERNS:
        if benign_pat.search(text):
            return False

    # 4. Check known exhaustion phrases and 402 payment markers
    for phrase in EXHAUSTION_PHRASES:
        if phrase in text_lower:
            return True
    if re.search(r"\b(?:status(?:_code)?|http|code|error|err)\s*[:=]?\s*402\b", text_lower):
        return True
    if re.search(r"\b402\s+(?:payment required|payment|credits?|wallet|exhausted)", text_lower):
        return True

    # Check args or response attributes if present
    args = getattr(error, "args", ())
    for arg in args:
        if isinstance(arg, (dict, str)) and is_failover_error(arg, seen=child_seen, depth=depth + 1):
            return True

    # Check chained causes (__cause__ or __context__) if present
    cause = getattr(error, "__cause__", None)
    if cause is not None and is_failover_error(cause, seen=child_seen, depth=depth + 1):
        return True
    context = getattr(error, "__context__", None)
    if context is not None and is_failover_error(context, seen=child_seen, depth=depth + 1):
        return True

    return False


def is_exhaustion_error(
    error: Any,
    seen: Optional[Set[int]] = None,
    depth: int = 0,
) -> bool:
    """Return True if error matches known quota exhaustion or upstream gateway failure signatures."""
    if depth > 10:
        return False
    if depth == 0 and CanopyClassifier is not None:
        try:
            if CanopyClassifier.is_canopy_exhaustion(error):
                return True
        except Exception:
            pass
    return is_failover_error(error, seen=seen, depth=depth)


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
                loaded_state = data.get("state", STATE_PRIMARY)
                if loaded_state in (STATE_PRIMARY, STATE_FAILOVER, STATE_PROBING):
                    self._state = loaded_state
                else:
                    self._state = STATE_PRIMARY

                raw_time = data.get("last_failure_time")
                if raw_time is None:
                    self._last_failure_time = None
                else:
                    try:
                        self._last_failure_time = float(raw_time)
                    except (ValueError, TypeError):
                        try:
                            if isinstance(raw_time, str):
                                from datetime import datetime
                                clean_ts = raw_time.replace("Z", "+00:00")
                                self._last_failure_time = float(datetime.fromisoformat(clean_ts).timestamp())
                            else:
                                self._last_failure_time = None
                        except Exception:
                            self._last_failure_time = None
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
            tmp_path = self.state_file.with_name(f"{self.state_file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp_path.replace(self.state_file)
        except Exception:
            pass

    @property
    def state(self) -> str:
        self._load_state()
        if self._state == STATE_FAILOVER and self._last_failure_time is not None:
            try:
                if self._time_fn() - float(self._last_failure_time) >= self.cooldown_seconds:
                    self._state = STATE_PROBING
                    self._save_state("cooldown_elapsed")
            except (ValueError, TypeError):
                self._state = STATE_PROBING
                self._last_failure_time = 0.0
                self._save_state("cooldown_elapsed_corrupt_ts")
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

    def __init__(
        self,
        circuit_breaker: Optional[AntigravityCircuitBreaker] = None,
        wallet_manager: Optional[WalletManager] = None,
        pause_manager: Optional[PauseManager] = None,
    ):
        super().__init__()
        self.circuit_breaker = circuit_breaker or AntigravityCircuitBreaker()
        self.wallet_manager = (
            wallet_manager
            if wallet_manager is not None
            else (WalletManager() if WalletManager is not None else None)
        )
        self.pause_manager = (
            pause_manager
            if pause_manager is not None
            else (PauseManager() if PauseManager is not None else None)
        )

    def _resolve_unstoppable_key(self) -> Optional[str]:
        """Dynamically resolve active healthy key from WalletManager with fallback."""
        if self.wallet_manager is not None:
            try:
                key = self.wallet_manager.get_active_key()
                if key:
                    return key
            except Exception as e:
                verbose_proxy_logger.warning("WalletManager.get_active_key failed: %s", e)
        return get_unstoppable_code_key()

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
            # Explicit Unstoppable Code AI credits routing:
            # Opus 5.5 -> DeepSeek V4 Pro
            # Sonnet 5.5 -> GLM 5.3 Flash (max reasoning)
            if is_unstoppable_opus_model(clean_check):
                target_unstoppable_model = "deepseek-v4-pro"
                data["model"] = target_unstoppable_model
                data["stream"] = True
                clean_token = self._resolve_unstoppable_key()
                if clean_token:
                    data["api_key"] = clean_token
                    if not isinstance(data.get("extra_headers"), dict):
                        data["extra_headers"] = {}
                    data["extra_headers"]["Authorization"] = f"Bearer {clean_token}"
                    data["extra_headers"]["Content-Type"] = "application/json"
                    data["extra_headers"]["anthropic-version"] = "2023-06-01"

                verbose_proxy_logger.warning(
                    "StripOrphanToolChoiceGuard: routed unstoppable ai credits model %s -> %s (dynamic auth, stream=True)",
                    current_model,
                    target_unstoppable_model,
                )
            elif is_unstoppable_sonnet_model(clean_check):
                target_unstoppable_model = "glm-5.3-flash"
                data["model"] = target_unstoppable_model
                data["stream"] = True
                data["reasoning_effort"] = "high"
                data["thinking"] = {"type": "enabled", "budget_tokens": 32000}
                clean_token = self._resolve_unstoppable_key()
                if clean_token:
                    data["api_key"] = clean_token
                    if not isinstance(data.get("extra_headers"), dict):
                        data["extra_headers"] = {}
                    data["extra_headers"]["Authorization"] = f"Bearer {clean_token}"
                    data["extra_headers"]["Content-Type"] = "application/json"
                    data["extra_headers"]["anthropic-version"] = "2023-06-01"

                verbose_proxy_logger.warning(
                    "StripOrphanToolChoiceGuard: routed unstoppable ai credits model %s -> %s (max reasoning, dynamic auth, stream=True)",
                    current_model,
                    target_unstoppable_model,
                )
            elif is_unstoppable_model(clean_check, data.get("api_base")):
                clean_token = self._resolve_unstoppable_key()
                if clean_token:
                    data["api_key"] = clean_token
                    if not isinstance(data.get("extra_headers"), dict):
                        data["extra_headers"] = {}
                    data["extra_headers"]["Authorization"] = f"Bearer {clean_token}"
                    data["extra_headers"]["Content-Type"] = "application/json"
                    data["extra_headers"]["anthropic-version"] = "2023-06-01"
                    data["stream"] = True
                verbose_proxy_logger.warning(
                    "StripOrphanToolChoiceGuard: preserving unstoppable ai credits model %s (dynamic auth, stream=True)",
                    current_model,
                )
            elif (
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

    async def async_pre_request_hook(
        self, model: str, messages: list[dict], request_kwargs: dict
    ) -> dict:
        """Called directly inside anthropic_messages handler before sending upstream."""
        api_base = request_kwargs.get("api_base")
        if is_unstoppable_opus_model(model) or (api_base and "unstoppable.ai" in api_base and "deepseek" in str(model)):
            clean_token = self._resolve_unstoppable_key()
            if clean_token:
                if not isinstance(request_kwargs.get("extra_headers"), dict):
                    request_kwargs["extra_headers"] = {}
                request_kwargs["extra_headers"]["Authorization"] = f"Bearer {clean_token}"
                request_kwargs["extra_headers"]["Content-Type"] = "application/json"
                request_kwargs["extra_headers"]["anthropic-version"] = "2023-06-01"
                request_kwargs["stream"] = True
                verbose_proxy_logger.warning(
                    "StripOrphanToolChoiceGuard [pre_request_hook]: routed unstoppable ai credits model %s -> deepseek-v4-pro (dynamic auth, stream=True)",
                    model,
                )
        elif is_unstoppable_sonnet_model(model) or (api_base and "unstoppable.ai" in api_base and "glm" in str(model)):
            clean_token = self._resolve_unstoppable_key()
            if clean_token:
                if not isinstance(request_kwargs.get("extra_headers"), dict):
                    request_kwargs["extra_headers"] = {}
                request_kwargs["extra_headers"]["Authorization"] = f"Bearer {clean_token}"
                request_kwargs["extra_headers"]["Content-Type"] = "application/json"
                request_kwargs["extra_headers"]["anthropic-version"] = "2023-06-01"
                request_kwargs["stream"] = True
                request_kwargs["reasoning_effort"] = "high"
                request_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 32000}
                verbose_proxy_logger.warning(
                    "StripOrphanToolChoiceGuard [pre_request_hook]: routed unstoppable ai credits model %s -> glm-5.3-flash (max reasoning, dynamic auth, stream=True)",
                    model,
                )
        elif is_unstoppable_model(model, api_base):
            clean_token = self._resolve_unstoppable_key()
            if clean_token:
                if not isinstance(request_kwargs.get("extra_headers"), dict):
                    request_kwargs["extra_headers"] = {}
                request_kwargs["extra_headers"]["Authorization"] = f"Bearer {clean_token}"
                request_kwargs["extra_headers"]["Content-Type"] = "application/json"
                request_kwargs["extra_headers"]["anthropic-version"] = "2023-06-01"
                request_kwargs["stream"] = True
                verbose_proxy_logger.warning(
                    "StripOrphanToolChoiceGuard [pre_request_hook]: injected dynamic auth & stream: True for %s",
                    model,
                )
        return request_kwargs

    async def async_pre_call_deployment_hook(
        self, kwargs: dict[str, Any], call_type: Any = None
    ) -> Optional[dict]:
        """Sanitize deployment kwargs and enforce fast-fail 402 backpressure before dispatch."""
        if not isinstance(kwargs, dict):
            return kwargs

        # Strip call_type and route_type if present
        for bad_param in ("call_type", "route_type"):
            if bad_param in kwargs:
                kwargs.pop(bad_param, None)

        # Strip orphan tool_choice / parallel_tool_calls if tools is empty or None
        if "tool_choice" in kwargs or "parallel_tool_calls" in kwargs:
            tools = kwargs.get("tools")
            if not self._has_tools(tools):
                kwargs.pop("tool_choice", None)
                kwargs.pop("parallel_tool_calls", None)
                verbose_proxy_logger.debug(
                    "StripOrphanToolChoiceGuard [deployment_hook]: removed tool_choice without tools"
                )

        clean_model = kwargs.get("model")
        api_base = kwargs.get("api_base")

        if is_unstoppable_model(clean_model, api_base):
            # 1. Fast-fail check: verify if PauseManager has active pause lock
            if self.pause_manager is not None and self.pause_manager.is_paused():
                pause_state = self.pause_manager.get_pause_state()
                corr_id = (
                    getattr(pause_state, "correlation_id", None)
                    if pause_state
                    else None
                )
                reason = getattr(pause_state, "reason", None) if pause_state else None

                verbose_proxy_logger.warning(
                    "StripOrphanToolChoiceGuard [deployment_hook]: Fast-failing paused Unstoppable request (corr_id=%s, reason=%s)",
                    corr_id,
                    reason,
                )
                if ErrorEnricher is not None:
                    raise ErrorEnricher.format_http_exception(
                        correlation_id=corr_id, reason=reason
                    )
                else:
                    from fastapi import HTTPException

                    raise HTTPException(
                        status_code=402,
                        detail={
                            "error": {
                                "message": (
                                    "[Unstoppable AI Resource Depleted] All configured prepaid wallets have zero credit. "
                                    "Processing is paused while the background probe monitors fund replenishment."
                                ),
                                "type": "payment_required",
                                "code": 402,
                                "canopy_correlation_id": corr_id or "req_canopy_paused",
                                "status": "paused",
                                "resume_instruction": (
                                    "Run 'claude --resume' once funds are replenished or after receiving the desktop notification."
                                ),
                            }
                        },
                    )

            # 2. Dynamic active key resolution from WalletManager
            clean_token = self._resolve_unstoppable_key()
            if (
                not clean_token
                and self.wallet_manager is not None
                and not self.wallet_manager.has_active_wallet()
            ):
                # Keystore is depleted: trigger pause and fast-fail
                if self.pause_manager is not None:
                    self.pause_manager.pause(reason="All wallets exhausted (no active key)")
                    if hasattr(self.pause_manager, "ensure_probe_daemon_running"):
                        self.pause_manager.ensure_probe_daemon_running()
                if ErrorEnricher is not None:
                    raise ErrorEnricher.format_http_exception(reason="All wallets exhausted")
                else:
                    from fastapi import HTTPException

                    raise HTTPException(
                        status_code=402,
                        detail={
                            "error": {
                                "message": (
                                    "[Unstoppable AI Resource Depleted] All configured prepaid wallets have zero credit. "
                                    "Processing is paused while the background probe monitors fund replenishment."
                                ),
                                "type": "payment_required",
                                "code": 402,
                                "canopy_correlation_id": "req_canopy_exhausted",
                                "status": "paused",
                                "resume_instruction": (
                                    "Run 'claude --resume' once funds are replenished or after receiving the desktop notification."
                                ),
                            }
                        },
                    )

            # 3. Dynamic active key injection and mandatory stream: True
            if clean_token:
                kwargs["api_key"] = clean_token
                if not isinstance(kwargs.get("extra_headers"), dict):
                    kwargs["extra_headers"] = {}
                kwargs["extra_headers"]["Authorization"] = f"Bearer {clean_token}"
                kwargs["extra_headers"]["Content-Type"] = "application/json"
                kwargs["extra_headers"]["anthropic-version"] = "2023-06-01"
                kwargs["stream"] = True

                if is_unstoppable_opus_model(clean_model) or (
                    api_base and "unstoppable.ai" in api_base and "deepseek" in str(clean_model)
                ):
                    kwargs["model"] = "anthropic/deepseek-v4-pro"
                elif is_unstoppable_sonnet_model(clean_model) or (
                    api_base and "unstoppable.ai" in api_base and "glm" in str(clean_model)
                ):
                    kwargs["model"] = "anthropic/glm-5.3-flash"
                    kwargs["reasoning_effort"] = "high"
                    kwargs["thinking"] = {"type": "enabled", "budget_tokens": 32000}

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

        api_base = (
            request_data.get("api_base")
            if isinstance(request_data, dict)
            else kwargs.get("api_base")
        )

        # Canopy credit exhaustion check for Unstoppable endpoints
        is_canopy = False
        if CanopyClassifier is not None:
            is_canopy = CanopyClassifier.is_canopy_exhaustion(original_exception)
        else:
            is_canopy = is_failover_error(original_exception)

        if is_canopy and is_unstoppable_model(model, api_base):
            corr_id = None
            reason = None
            if CanopyClassifier is not None:
                corr_id = CanopyClassifier.extract_correlation_id(original_exception)
                reason = CanopyClassifier.extract_exhaustion_reason(original_exception)
            reason = reason or "personal_wallet_empty"

            verbose_proxy_logger.warning(
                "StripOrphanToolChoiceGuard: Canopy credit exhaustion detected (model=%s, corr_id=%s, reason=%s)",
                model,
                corr_id,
                reason,
            )

            # In-flight wallet rotation
            if self.wallet_manager is not None:
                # Milestone 5 It2: Extract request-bound credential to prevent concurrent over-rotation
                token: Optional[str] = None
                if isinstance(request_data, dict):
                    token = request_data.get("api_key")
                    if not token:
                        for hdr_dict_name in ("extra_headers", "headers"):
                            hdrs = request_data.get(hdr_dict_name)
                            if isinstance(hdrs, dict):
                                for k, v in hdrs.items():
                                    if str(k).lower() == "authorization" and isinstance(v, str):
                                        val = v.strip()
                                        token = val[7:].strip() if val.lower().startswith("bearer ") else val
                                        break
                                if token:
                                    break

                if not token and kwargs:
                    token = kwargs.get("api_key")
                    if not token:
                        for hdr_dict_name in ("extra_headers", "headers"):
                            hdrs = kwargs.get(hdr_dict_name)
                            if isinstance(hdrs, dict):
                                for k, v in hdrs.items():
                                    if str(k).lower() == "authorization" and isinstance(v, str):
                                        val = v.strip()
                                        token = val[7:].strip() if val.lower().startswith("bearer ") else val
                                        break
                                if token:
                                    break

                failed_wallet = None
                if token:
                    clean_token = str(token).strip()
                    if hasattr(self.wallet_manager, "get_wallet_by_key"):
                        failed_wallet = self.wallet_manager.get_wallet_by_key(clean_token)
                    elif hasattr(self.wallet_manager, "get_all_wallets"):
                        for w in self.wallet_manager.get_all_wallets():
                            if getattr(w, "key", None) == clean_token:
                                failed_wallet = w
                                break

                target_wid = failed_wallet.id if failed_wallet else self.wallet_manager.active_wallet_id
                next_wallet = self.wallet_manager.mark_exhausted(target_wid)
                if next_wallet is not None:
                    verbose_proxy_logger.info(
                        "StripOrphanToolChoiceGuard: Rotated active wallet %s -> %s",
                        target_wid,
                        next_wallet.id,
                    )
                else:
                    verbose_proxy_logger.critical(
                        "StripOrphanToolChoiceGuard: All wallets exhausted in keystore! Triggering pause."
                    )
                    if self.pause_manager is not None:
                        self.pause_manager.pause(
                            reason=f"All wallets exhausted ({reason})",
                            correlation_id=corr_id,
                        )
                        if hasattr(self.pause_manager, "ensure_probe_daemon_running"):
                            self.pause_manager.ensure_probe_daemon_running()
            else:
                if self.pause_manager is not None:
                    self.pause_manager.pause(
                        reason=f"Canopy credit exhaustion ({reason})",
                        correlation_id=corr_id,
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
_wallet_manager: Optional[WalletManager] = (
    WalletManager() if WalletManager is not None else None
)
_pause_manager: Optional[PauseManager] = (
    PauseManager() if PauseManager is not None else None
)
proxy_guard = StripOrphanToolChoiceGuard(
    circuit_breaker=circuit_breaker,
    wallet_manager=_wallet_manager,
    pause_manager=_pause_manager,
)
