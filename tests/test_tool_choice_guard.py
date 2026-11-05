import asyncio
import litellm
import pytest
import sys
from pathlib import Path

# Add ~/.litellm and repository root to python path (repo_root takes priority)
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path.home() / ".litellm"))
sys.path.insert(0, str(repo_root))

from tool_choice_guard import (
    AntigravityCircuitBreaker,
    StripOrphanToolChoiceGuard,
    is_exhaustion_error,
    is_failover_error,
    is_unstoppable_model,
    is_unstoppable_opus_model,
    is_unstoppable_sonnet_model,
    get_unstoppable_code_key,
    STATE_PRIMARY,
    STATE_FAILOVER,
    STATE_PROBING,
)


@pytest.mark.asyncio
async def test_strip_unsupported_stop_sequences_in_responses_api():
    guard = StripOrphanToolChoiceGuard()
    payload = {
        "model": "azure/gpt-5.4",
        "input": [{"type": "message", "role": "user", "content": "hello"}],
        "stop_sequences": ["\n\nHuman:"],
    }
    result = await guard.async_pre_call_hook(
        user_api_key_dict={},
        cache=None,
        data=payload,
        call_type="aresponses",
    )
    assert "stop_sequences" not in result, "stop_sequences must be stripped for Responses API"


@pytest.mark.asyncio
async def test_strip_tool_choice_when_tools_is_empty_or_none():
    guard = StripOrphanToolChoiceGuard()
    payload = {
        "model": "azure/gpt-5.4",
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    result = await guard.async_pre_call_hook(
        user_api_key_dict={},
        cache=None,
        data=payload,
        call_type="aresponses",
    )
    assert "tool_choice" not in result
    assert "parallel_tool_calls" not in result


@pytest.mark.asyncio
async def test_retain_tool_choice_when_tools_are_present():
    guard = StripOrphanToolChoiceGuard()
    payload = {
        "model": "azure/gpt-5.4",
        "tools": [{"type": "function", "name": "do_work"}],
        "tool_choice": "auto",
    }
    result = await guard.async_pre_call_hook(
        user_api_key_dict={},
        cache=None,
        data=payload,
        call_type="aresponses",
    )
    assert result.get("tool_choice") == "auto"
    assert len(result.get("tools")) == 1


@pytest.mark.asyncio
async def test_sanitize_responses_input_drops_orphaned_function_call_output():
    guard = StripOrphanToolChoiceGuard()
    # Simulate an input list where auto-compact truncated the preceding function_call
    # leaving an orphaned function_call_output
    payload = {
        "model": "azure/gpt-5.4",
        "input": [
            {"type": "message", "role": "user", "content": "summarize repo"},
            {
                "type": "function_call_output",
                "call_id": "call_orphaned_123",
                "output": "some result",
            },
            {
                "type": "function_call",
                "call_id": "call_valid_456",
                "name": "read_file",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_valid_456",
                "output": "file content",
            },
        ],
    }
    result = await guard.async_pre_call_hook(
        user_api_key_dict={},
        cache=None,
        data=payload,
        call_type="aresponses",
    )
    sanitized_input = result["input"]
    # The orphaned item should be removed
    call_ids = [item.get("call_id") for item in sanitized_input if "call_id" in item]
    assert "call_orphaned_123" not in call_ids, "Orphaned function_call_output must be stripped"
    assert "call_valid_456" in call_ids, "Valid paired function_call must be preserved"
    assert len(sanitized_input) == 3


@pytest.mark.asyncio
async def test_strip_call_type_from_request_data():
    guard = StripOrphanToolChoiceGuard()
    payload = {
        "model": "azure/gpt-5.4",
        "call_type": "anthropic_messages",
        "messages": [{"role": "user", "content": "hello"}],
    }
    result = await guard.async_pre_call_hook(
        user_api_key_dict={},
        cache=None,
        data=payload,
        call_type="anthropic_messages",
    )
    assert "call_type" not in result, "call_type must be stripped to prevent Azure 400 error"


@pytest.mark.asyncio
async def test_strip_tool_choice_in_anthropic_messages_when_no_tools():
    guard = StripOrphanToolChoiceGuard()
    payload = {
        "model": "azure/gpt-5.4",
        "tools": [],
        "tool_choice": {"type": "auto"},
        "messages": [{"role": "user", "content": "search web"}],
    }
    result = await guard.async_pre_call_hook(
        user_api_key_dict={},
        cache=None,
        data=payload,
        call_type="anthropic_messages",
    )
    assert "tool_choice" not in result, "tool_choice must be stripped when tools is empty in anthropic_messages"


@pytest.mark.asyncio
async def test_async_pre_call_deployment_hook_strips_orphan_tool_choice():
    guard = StripOrphanToolChoiceGuard()
    kwargs = {
        "model": "azure/gpt-5.4",
        "call_type": "acompletion",
        "tool_choice": "auto",
        "tools": None,
        "web_search_options": {},
    }
    result = await guard.async_pre_call_deployment_hook(kwargs)
    assert "call_type" not in result
    assert "tool_choice" not in result
    assert result.get("web_search_options") == {}


@pytest.mark.asyncio
async def test_strip_route_type_from_request_data_and_deployment():
    guard = StripOrphanToolChoiceGuard()
    # Test pre_call hook
    payload = {
        "model": "azure/gpt-5.4",
        "call_type": "anthropic_messages",
        "route_type": "anthropic_messages",
        "messages": [{"role": "user", "content": "hello"}],
    }
    pre_result = await guard.async_pre_call_hook(
        user_api_key_dict={},
        cache=None,
        data=payload,
        call_type="anthropic_messages",
    )
    assert "call_type" not in pre_result
    assert "route_type" not in pre_result

    # Test deployment hook
    kwargs = {
        "model": "azure/gpt-5.4",
        "call_type": "acompletion",
        "route_type": "acompletion",
        "tools": None,
        "tool_choice": "auto",
    }
    dep_result = await guard.async_pre_call_deployment_hook(kwargs)
    assert "call_type" not in dep_result
    assert "route_type" not in dep_result
    assert "tool_choice" not in dep_result


@pytest.mark.asyncio
async def test_detect_reasoning_effort_anthropic_thinking():
    guard = StripOrphanToolChoiceGuard()
    # budget_tokens <= 2048 -> "low"
    assert guard._detect_reasoning_effort({"thinking": {"type": "enabled", "budget_tokens": 1024}}) == "low"
    assert guard._detect_reasoning_effort({"thinking": {"type": "enabled", "budget_tokens": 2048}}) == "low"
    # 2048 < budget_tokens <= 10000 -> "medium"
    assert guard._detect_reasoning_effort({"thinking": {"type": "enabled", "budget_tokens": 2049}}) == "medium"
    assert guard._detect_reasoning_effort({"thinking": {"type": "enabled", "budget_tokens": 5000}}) == "medium"
    assert guard._detect_reasoning_effort({"thinking": {"type": "enabled", "budget_tokens": 10000}}) == "medium"
    # budget_tokens > 10000 or type == "adaptive" -> "high"
    assert guard._detect_reasoning_effort({"thinking": {"type": "enabled", "budget_tokens": 10001}}) == "high"
    assert guard._detect_reasoning_effort({"thinking": {"type": "enabled", "budget_tokens": 16000}}) == "high"
    assert guard._detect_reasoning_effort({"thinking": {"type": "adaptive"}}) == "high"
    # disabled / absent -> "default"
    assert guard._detect_reasoning_effort({"thinking": {"type": "disabled"}}) == "default"
    assert guard._detect_reasoning_effort({"thinking": None}) == "default"
    assert guard._detect_reasoning_effort({}) == "default"
    assert guard._detect_reasoning_effort(None) == "default"


@pytest.mark.asyncio
async def test_detect_reasoning_effort_openai():
    guard = StripOrphanToolChoiceGuard()
    # reasoning_effort: "low"/"minimal" -> "low"
    assert guard._detect_reasoning_effort({"reasoning_effort": "low"}) == "low"
    assert guard._detect_reasoning_effort({"reasoning_effort": "minimal"}) == "low"
    # "medium" -> "medium"
    assert guard._detect_reasoning_effort({"reasoning_effort": "medium"}) == "medium"
    # "high"/"xhigh"/"max" -> "high"
    assert guard._detect_reasoning_effort({"reasoning_effort": "high"}) == "high"
    assert guard._detect_reasoning_effort({"reasoning_effort": "xhigh"}) == "high"
    assert guard._detect_reasoning_effort({"reasoning_effort": "max"}) == "high"
    # reasoning: {"effort": "low"} -> "low"
    assert guard._detect_reasoning_effort({"reasoning": {"effort": "low"}}) == "low"
    assert guard._detect_reasoning_effort({"reasoning": {"effort": "minimal"}}) == "low"
    assert guard._detect_reasoning_effort({"reasoning": {"effort": "medium"}}) == "medium"
    assert guard._detect_reasoning_effort({"reasoning": {"effort": "high"}}) == "high"
    assert guard._detect_reasoning_effort({"reasoning": {"effort": "xhigh"}}) == "high"
    assert guard._detect_reasoning_effort({"reasoning": {"effort": "max"}}) == "high"


@pytest.mark.asyncio
async def test_resolve_antigravity_target():
    guard = StripOrphanToolChoiceGuard()
    # claude-haiku-* with default/low -> "antigravity-flash-low"
    assert guard._resolve_antigravity_target("claude-haiku-4-5-20251001", "default") == "antigravity-flash-low"
    assert guard._resolve_antigravity_target("claude-haiku-4-5", "low") == "antigravity-flash-low"
    # claude-haiku-* with high -> "antigravity-flash-high"
    assert guard._resolve_antigravity_target("claude-haiku-4-5-20251001", "high") == "antigravity-flash-high"
    assert guard._resolve_antigravity_target("claude-haiku-4-5", "medium") == "antigravity-flash-medium"

    # claude-gpt-5, claude-sonnet-*, gpt-5.4, gpt-* with default/high -> "antigravity-flash-high"
    assert guard._resolve_antigravity_target("claude-gpt-5", "default") == "antigravity-flash-high"
    assert guard._resolve_antigravity_target("claude-gpt-5", "high") == "antigravity-flash-high"
    assert guard._resolve_antigravity_target("claude-sonnet-5", "default") == "antigravity-flash-high"
    assert guard._resolve_antigravity_target("claude-sonnet-4-6", "high") == "antigravity-flash-high"
    assert guard._resolve_antigravity_target("gpt-5.4", "default") == "antigravity-flash-high"
    assert guard._resolve_antigravity_target("gpt-5.4", "high") == "antigravity-flash-high"
    assert guard._resolve_antigravity_target("gpt-4o", "default") == "antigravity-flash-high"
    assert guard._resolve_antigravity_target("gpt-5", "high") == "antigravity-flash-high"

    # with low -> "antigravity-flash-low"
    assert guard._resolve_antigravity_target("claude-gpt-5", "low") == "antigravity-flash-low"
    assert guard._resolve_antigravity_target("claude-sonnet-5", "low") == "antigravity-flash-low"
    assert guard._resolve_antigravity_target("gpt-5.4", "low") == "antigravity-flash-low"
    assert guard._resolve_antigravity_target("gpt-4o", "low") == "antigravity-flash-low"

    # with medium -> "antigravity-flash-medium"
    assert guard._resolve_antigravity_target("claude-gpt-5", "medium") == "antigravity-flash-medium"
    assert guard._resolve_antigravity_target("claude-sonnet-4-6", "medium") == "antigravity-flash-medium"
    assert guard._resolve_antigravity_target("gpt-5.4", "medium") == "antigravity-flash-medium"
    assert guard._resolve_antigravity_target("gpt-4o", "medium") == "antigravity-flash-medium"

    # claude-opus-* and claude-mythos-* with any effort -> "antigravity-opus-4.6"
    assert guard._resolve_antigravity_target("claude-opus-4-6", "default") == "antigravity-opus-4.6"
    assert guard._resolve_antigravity_target("claude-opus-4-6", "low") == "antigravity-opus-4.6"
    assert guard._resolve_antigravity_target("claude-opus-4-6", "medium") == "antigravity-opus-4.6"
    assert guard._resolve_antigravity_target("claude-opus-4-6", "high") == "antigravity-opus-4.6"
    assert guard._resolve_antigravity_target("claude-mythos-5", "default") == "antigravity-opus-4.6"
    assert guard._resolve_antigravity_target("claude-mythos-5", "high") == "antigravity-opus-4.6"
    assert guard._resolve_antigravity_target("claude-mythos-5", "low") == "antigravity-opus-4.6"


@pytest.mark.asyncio
async def test_async_pre_call_hook_rewrites_model_and_injects_reasoning_effort():
    guard = StripOrphanToolChoiceGuard()
    # Claude route with thinking low
    payload_claude = {
        "model": "claude-gpt-5",
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "messages": [{"role": "user", "content": "hello"}],
    }
    res_claude = await guard.async_pre_call_hook(
        user_api_key_dict={}, cache=None, data=payload_claude, call_type="anthropic_messages"
    )
    assert res_claude["model"] == "antigravity-flash-low"
    assert res_claude["reasoning_effort"] == "low"

    # Claude route with default (no thinking) -> antigravity-flash-high
    payload_default = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "hello"}],
    }
    res_default = await guard.async_pre_call_hook(
        user_api_key_dict={}, cache=None, data=payload_default, call_type="anthropic_messages"
    )
    assert res_default["model"] == "antigravity-flash-high"
    assert res_default["reasoning_effort"] == "high"

    # Haiku with default -> antigravity-flash-low
    payload_haiku = {
        "model": "claude-haiku-4-5-20251001",
        "messages": [{"role": "user", "content": "hello"}],
    }
    res_haiku = await guard.async_pre_call_hook(
        user_api_key_dict={}, cache=None, data=payload_haiku, call_type="anthropic_messages"
    )
    assert res_haiku["model"] == "antigravity-flash-low"
    assert res_haiku["reasoning_effort"] == "low"

    # Opus with medium effort -> antigravity-opus-4.6
    payload_opus = {
        "model": "claude-opus-4-6",
        "thinking": {"type": "enabled", "budget_tokens": 5000},
        "messages": [{"role": "user", "content": "hello"}],
    }
    res_opus = await guard.async_pre_call_hook(
        user_api_key_dict={}, cache=None, data=payload_opus, call_type="anthropic_messages"
    )
    assert res_opus["model"] == "antigravity-opus-4.6"
    assert res_opus["reasoning_effort"] == "medium"

    # OpenAI / Codex route with reasoning_effort
    payload_codex = {
        "model": "gpt-5.4",
        "reasoning_effort": "low",
        "input": [{"type": "message", "role": "user", "content": "hello"}],
    }
    res_codex = await guard.async_pre_call_hook(
        user_api_key_dict={}, cache=None, data=payload_codex, call_type="aresponses"
    )
    assert res_codex["model"] == "antigravity-flash-low"
    assert res_codex["reasoning_effort"] == "low"

    # Preserves sanitization: call_type stripped, stop_sequences stripped in Responses API
    payload_sanitized = {
        "model": "gpt-5.4",
        "call_type": "aresponses",
        "stop_sequences": ["\n\nHuman:"],
        "input": [
            {"type": "message", "role": "user", "content": "hello"},
            {"type": "function_call_output", "call_id": "orphaned_1", "output": "bad"},
        ],
    }
    res_sanitized = await guard.async_pre_call_hook(
        user_api_key_dict={}, cache=None, data=payload_sanitized, call_type="aresponses"
    )
    assert res_sanitized["model"] == "antigravity-flash-high"
    assert res_sanitized["reasoning_effort"] == "high"
    assert "call_type" not in res_sanitized
    assert "stop_sequences" not in res_sanitized
    assert len(res_sanitized["input"]) == 1


# ----------------------------------------------------------------------
# AntigravityCircuitBreaker & is_exhaustion_error Unit Tests
# ----------------------------------------------------------------------


def test_is_exhaustion_error_detects_429():
    class DummyHTTPError(Exception):
        def __init__(self, msg: str, status_code: int):
            super().__init__(msg)
            self.status_code = status_code

    assert is_exhaustion_error(DummyHTTPError("Too Many Requests", 429)) is True
    assert is_exhaustion_error(Exception("HTTP 429 Too Many Requests")) is True
    assert is_exhaustion_error("Client error: 429 Too Many Requests") is True
    assert is_exhaustion_error("status_code: 429") is True
    assert is_exhaustion_error({"error_code": 429, "message": "rate limit"}) is True
    assert is_exhaustion_error({"status_code": 429}) is True
    assert is_exhaustion_error({"code": 429}) is True
    assert is_exhaustion_error({"error": {"code": 429, "message": "Too many requests"}}) is True
    assert is_exhaustion_error('{"error_code": 429, "message": "rate limit"}') is True


def test_is_exhaustion_error_detects_resource_exhausted():
    assert is_exhaustion_error(Exception("RESOURCE_EXHAUSTED")) is True
    assert is_exhaustion_error(Exception("Error: resource_exhausted from backend")) is True
    assert is_exhaustion_error("Resource_Exhausted: Quota exceeded") is True
    assert is_exhaustion_error({"status": "RESOURCE_EXHAUSTED"}) is True
    assert is_exhaustion_error({"status": "resource_exhausted"}) is True
    assert is_exhaustion_error({"error": {"status": "RESOURCE_EXHAUSTED"}}) is True
    assert is_exhaustion_error('{"status": "RESOURCE_EXHAUSTED"}') is True


def test_is_exhaustion_error_detects_ai_credits_balance():
    msg = "Your AI credits balance is too low to continue"
    assert is_exhaustion_error(Exception(msg)) is True
    assert is_exhaustion_error(Exception(f"Error 400: {msg}. Please upgrade.")) is True
    assert is_exhaustion_error("your ai credits balance is too low to continue") is True
    assert is_exhaustion_error({"message": msg}) is True


def test_is_exhaustion_error_detects_resource_has_been_exhausted():
    msg = "Resource has been exhausted (e.g. check quota)"
    assert is_exhaustion_error(Exception(msg)) is True
    assert is_exhaustion_error("Resource has been exhausted (e.g. check quota)") is True
    assert is_exhaustion_error("Resource has been exhausted") is True
    assert is_exhaustion_error({"error": {"message": msg}}) is True


def test_is_exhaustion_error_rejects_unrelated_errors():
    assert is_exhaustion_error(Exception("404 Not Found")) is False
    assert is_exhaustion_error(Exception("500 Internal Server Error")) is False
    assert is_exhaustion_error(SyntaxError("invalid syntax on line 42")) is False
    assert is_exhaustion_error(SyntaxError("invalid syntax line 429")) is False
    assert is_exhaustion_error(ValueError("bad value")) is False
    assert is_exhaustion_error({"status_code": 404, "message": "Not Found"}) is False
    assert is_exhaustion_error({"status_code": 500, "message": "Internal Server Error"}) is False
    assert is_exhaustion_error({"status": "SUCCESS"}) is False
    assert is_exhaustion_error(None) is False
    assert is_exhaustion_error("") is False


def test_circuit_breaker_default_state():
    cb = AntigravityCircuitBreaker()
    assert cb.state == STATE_PRIMARY
    assert cb.state == "primary"
    assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-flash-high"


def test_circuit_breaker_failover_model_mapping():
    cb = AntigravityCircuitBreaker()
    cb.trigger_failover()
    assert cb.state == STATE_FAILOVER
    assert cb.state == "failover"
    assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-remote-flash-high"
    assert cb.get_target_for_model("antigravity-flash-low") == "antigravity-remote-flash-low"
    assert cb.get_target_for_model("antigravity-flash-medium") == "antigravity-remote-flash-medium"
    assert cb.get_target_for_model("antigravity-opus-4.6") == "antigravity-remote-opus-4.6"
    # Remote model stays remote
    assert cb.get_target_for_model("antigravity-remote-flash-high") == "antigravity-remote-flash-high"
    # Unrelated model stays as is
    assert cb.get_target_for_model("glm5.2") == "glm5.2"


def test_circuit_breaker_cooldown_and_probing_transition():
    current_time = 1000.0
    cb = AntigravityCircuitBreaker(cooldown_seconds=900.0, time_fn=lambda: current_time)

    # Initial state
    assert cb.state == STATE_PRIMARY

    # Trigger failover
    cb.record_failure(Exception("HTTP 429 Too Many Requests"))
    assert cb.state == STATE_FAILOVER

    # When elapsed time < 900s, stays in STATE_FAILOVER
    current_time = 1000.0 + 899.0
    assert cb.state == STATE_FAILOVER
    assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-remote-flash-high"

    # When elapsed time >= 900s, transitions to STATE_PROBING and returns base target
    current_time = 1000.0 + 900.0
    assert cb.state == STATE_PROBING
    assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-flash-high"


def test_circuit_breaker_probe_success_transitions_to_primary():
    current_time = 1000.0
    cb = AntigravityCircuitBreaker(cooldown_seconds=900.0, time_fn=lambda: current_time)
    cb.trigger_failover()
    assert cb.state == STATE_FAILOVER

    # Fast forward past cooldown to probing
    current_time += 900.0
    assert cb.state == STATE_PROBING

    # Successful probe transitions back to primary
    cb.record_success()
    assert cb.state == STATE_PRIMARY
    assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-flash-high"


def test_circuit_breaker_probe_failure_resets_cooldown_and_returns_to_failover():
    current_time = 1000.0
    cb = AntigravityCircuitBreaker(cooldown_seconds=900.0, time_fn=lambda: current_time)
    cb.trigger_failover()

    # Fast forward past cooldown
    current_time += 900.0
    assert cb.state == STATE_PROBING

    # Failed probe with exhaustion error
    cb.record_failure(Exception("RESOURCE_EXHAUSTED"))
    assert cb.state == STATE_FAILOVER

    # Cooldown timer was reset to 1900.0; at 1900 + 899 = 2799, still failover
    current_time += 899.0
    assert cb.state == STATE_FAILOVER
    assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-remote-flash-high"

    # At 2800.0, transitions back to probing
    current_time += 1.0
    assert cb.state == STATE_PROBING
    assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-flash-high"


def test_circuit_breaker_subsequent_failure_in_failover_resets_cooldown():
    current_time = 1000.0
    cb = AntigravityCircuitBreaker(cooldown_seconds=900.0, time_fn=lambda: current_time)
    cb.trigger_failover()
    assert cb.state == STATE_FAILOVER

    # 500s later, another failure arrives while still in failover
    current_time += 500.0
    cb.record_failure(Exception("Your AI credits balance is too low to continue"))

    # Cooldown reset to 1500.0; at 1500 + 899 = 2399, still failover
    current_time += 899.0
    assert cb.state == STATE_FAILOVER

    # At 1500 + 900 = 2400, transitions to probing
    current_time += 1.0
    assert cb.state == STATE_PROBING


def test_circuit_breaker_ignores_non_exhaustion_errors():
    cb = AntigravityCircuitBreaker()
    cb.record_failure(Exception("404 Not Found"))
    assert cb.state == STATE_PRIMARY

    cb.record_failure(Exception("500 Internal Server Error"))
    assert cb.state == STATE_PRIMARY


@pytest.mark.asyncio
async def test_guard_integration_pre_call_hook_routes_to_remote_in_failover():
    guard = StripOrphanToolChoiceGuard()
    guard.circuit_breaker.trigger_failover()
    assert guard.circuit_breaker.state == STATE_FAILOVER

    # Claude request that resolves to flash-high should now be routed to remote
    payload_claude = {
        "model": "claude-gpt-5",
        "messages": [{"role": "user", "content": "hello"}],
    }
    res_claude = await guard.async_pre_call_hook(
        user_api_key_dict={}, cache=None, data=payload_claude, call_type="anthropic_messages"
    )
    assert res_claude["model"] == "antigravity-remote-flash-high"
    assert res_claude["reasoning_effort"] == "high"

    # Direct antigravity model should also be routed to remote
    payload_direct = {
        "model": "antigravity-opus-4.6",
        "messages": [{"role": "user", "content": "hello"}],
    }
    res_direct = await guard.async_pre_call_hook(
        user_api_key_dict={}, cache=None, data=payload_direct, call_type="anthropic_messages"
    )
    assert res_direct["model"] == "antigravity-remote-opus-4.6"


@pytest.mark.asyncio
async def test_circuit_breaker_state_file_persistence(tmp_path):
    state_file = tmp_path / "failover_state.json"
    cb1 = AntigravityCircuitBreaker(state_file=state_file)
    assert cb1.state == STATE_PRIMARY

    cb1.trigger_failover("test RESOURCE_EXHAUSTED 429")
    assert cb1.state == STATE_FAILOVER
    assert state_file.exists()

    # Second instance should read persisted state
    cb2 = AntigravityCircuitBreaker(state_file=state_file)
    assert cb2.state == STATE_FAILOVER

    # Reset in cb2 should persist to cb1
    cb2.reset()
    assert cb2.state == STATE_PRIMARY
    assert cb1.state == STATE_PRIMARY


@pytest.mark.asyncio
async def test_guard_integration_pre_call_hook_preserves_direct_remote_requests():
    guard = StripOrphanToolChoiceGuard()
    assert guard.circuit_breaker.state == STATE_PRIMARY

    payload_remote = {
        "model": "antigravity-remote-flash-high",
        "messages": [{"role": "user", "content": "hello"}],
    }
    res_remote = await guard.async_pre_call_hook(
        user_api_key_dict={}, cache=None, data=payload_remote, call_type="anthropic_messages"
    )
    assert res_remote["model"] == "antigravity-remote-flash-high"


@pytest.mark.asyncio
async def test_guard_integration_post_call_failure_triggers_circuit_breaker():
    guard = StripOrphanToolChoiceGuard()
    assert guard.circuit_breaker.state == STATE_PRIMARY

    # Exhaustion error should trigger circuit breaker
    await guard.async_post_call_failure_hook(
        request_data={"model": "antigravity-flash-high"},
        original_exception=Exception("Resource has been exhausted (e.g. check quota)"),
        user_api_key_dict={},
    )
    assert guard.circuit_breaker.state == STATE_FAILOVER


@pytest.mark.asyncio
async def test_guard_integration_post_call_failure_ignores_unrelated_errors():
    guard = StripOrphanToolChoiceGuard()
    assert guard.circuit_breaker.state == STATE_PRIMARY

    await guard.async_post_call_failure_hook(
        request_data={"model": "antigravity-flash-high"},
        original_exception=Exception("404 Not Found"),
        user_api_key_dict={},
    )
    assert guard.circuit_breaker.state == STATE_PRIMARY


@pytest.mark.asyncio
async def test_guard_integration_post_call_success_hook_records_success():
    guard = StripOrphanToolChoiceGuard()
    guard.circuit_breaker.trigger_failover()
    guard.circuit_breaker._state = STATE_PROBING
    assert guard.circuit_breaker.state == STATE_PROBING

    await guard.async_post_call_success_hook(
        data={"model": "antigravity-flash-high"},
        user_api_key_dict={},
        response={"choices": []},
    )
    assert guard.circuit_breaker.state == STATE_PRIMARY


@pytest.mark.asyncio
async def test_guard_routes_unstoppable_opus_to_deepseek_v4_pro():
    guard = StripOrphanToolChoiceGuard()
    for model_name in [
        "claude-opus-5.5",
        "claude-opus-5-5",
        "claude-opus-5-5[1m]",
        "opus-5.5",
        "opus 5.5",
        "opus[1m]",
        "anthropic/claude-opus-5.5",
        "deepseek-v4-pro",
    ]:
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "ping"}],
        }
        res = await guard.async_pre_call_hook(
            user_api_key_dict={}, cache=None, data=payload, call_type="anthropic_messages"
        )
        assert res["model"] == "deepseek-v4-pro", f"Expected deepseek-v4-pro for {model_name}, got {res['model']}"
        assert res.get("stream") is True, f"Expected stream=True for {model_name}"
        assert "Authorization" in res.get("extra_headers", {})
        assert res["extra_headers"]["Authorization"].startswith("Bearer csk_")
        assert res["extra_headers"]["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_guard_routes_unstoppable_sonnet_to_glm_5_3_flash_max_reasoning():
    guard = StripOrphanToolChoiceGuard()
    for model_name in [
        "claude-sonnet-5.5",
        "claude-sonnet-5-5",
        "claude-sonnet-5-5[1m]",
        "sonnet-5.5",
        "sonnet 5.5",
        "anthropic/claude-sonnet-5.5",
        "glm-5.3-flash",
    ]:
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "ping"}],
        }
        res = await guard.async_pre_call_hook(
            user_api_key_dict={}, cache=None, data=payload, call_type="anthropic_messages"
        )
        assert res["model"] == "glm-5.3-flash", f"Expected glm-5.3-flash for {model_name}, got {res['model']}"
        assert res.get("stream") is True, f"Expected stream=True for {model_name}"
        assert res.get("reasoning_effort") == "high", f"Expected high reasoning effort for {model_name}"
        assert res.get("thinking", {}).get("type") == "enabled"
        assert res.get("thinking", {}).get("budget_tokens") == 32000
        assert "Authorization" in res.get("extra_headers", {})
        assert res["extra_headers"]["Authorization"].startswith("Bearer csk_")
        assert res["extra_headers"]["anthropic-version"] == "2023-06-01"


def test_is_unstoppable_model_helpers():
    assert is_unstoppable_opus_model("claude-opus-5.5") is True
    assert is_unstoppable_opus_model("claude-opus-5-5") is True
    assert is_unstoppable_opus_model("claude-opus-5-5[1m]") is True
    assert is_unstoppable_opus_model("opus-5.5") is True
    assert is_unstoppable_opus_model("opus 5.5") is True
    assert is_unstoppable_opus_model("opus[1m]") is True
    assert is_unstoppable_opus_model("deepseek-v4-pro") is True

    assert is_unstoppable_sonnet_model("claude-sonnet-5.5") is True
    assert is_unstoppable_sonnet_model("claude-sonnet-5-5") is True
    assert is_unstoppable_sonnet_model("claude-sonnet-5-5[1m]") is True
    assert is_unstoppable_sonnet_model("sonnet-5.5") is True
    assert is_unstoppable_sonnet_model("sonnet 5.5") is True
    assert is_unstoppable_sonnet_model("glm-5.3-flash") is True

    assert is_unstoppable_model("claude-opus-5.5") is True
    assert is_unstoppable_model("claude-sonnet-5.5") is True
    assert is_unstoppable_model("custom-model", api_base="https://app.unstoppable.ai/api/v1/llm-proxy/anthropic") is True

    # Standard models must NOT be treated as unstoppable
    assert is_unstoppable_model("claude-3-7-sonnet") is False
    assert is_unstoppable_model("claude-mythos-5") is False
    assert is_unstoppable_model("claude-sonnet-5") is False
    assert is_unstoppable_model("gpt-4o") is False
    assert is_unstoppable_model("antigravity-flash") is False


def test_get_unstoppable_code_key():
    key = get_unstoppable_code_key()
    assert key is not None
    assert key.startswith("csk_")


@pytest.mark.asyncio
async def test_async_pre_request_hook_injects_unstoppable_auth_and_routes():
    guard = StripOrphanToolChoiceGuard()
    req_kwargs_opus = {"stream": False}
    result_opus = await guard.async_pre_request_hook(
        model="claude-opus-5.5",
        messages=[{"role": "user", "content": "hello"}],
        request_kwargs=req_kwargs_opus,
    )
    assert result_opus["stream"] is True
    assert "Authorization" in result_opus["extra_headers"]
    assert result_opus["extra_headers"]["Authorization"].startswith("Bearer csk_")
    assert result_opus["extra_headers"]["anthropic-version"] == "2023-06-01"

    req_kwargs_sonnet = {"stream": False}
    result_sonnet = await guard.async_pre_request_hook(
        model="claude-sonnet-5.5",
        messages=[{"role": "user", "content": "hello"}],
        request_kwargs=req_kwargs_sonnet,
    )
    assert result_sonnet["stream"] is True
    assert result_sonnet["reasoning_effort"] == "high"
    assert result_sonnet["thinking"]["budget_tokens"] == 32000
    assert "Authorization" in result_sonnet["extra_headers"]
    assert result_sonnet["extra_headers"]["Authorization"].startswith("Bearer csk_")
    assert result_sonnet["extra_headers"]["anthropic-version"] == "2023-06-01"

def test_is_exhaustion_error_rejects_prompt_or_benign_429_numbers():
    assert is_exhaustion_error(Exception("Connection failed to 127.0.0.1:4290")) is False
    assert is_exhaustion_error(Exception("Processed 429 tokens in 1.2s")) is False
    assert is_exhaustion_error(Exception("SyntaxError on line 429")) is False
    assert is_exhaustion_error({"prompt": "Can you explain HTTP status code 429?"}) is False
    assert is_exhaustion_error({"content": "Explain resource_exhausted in gRPC"}) is False


@pytest.mark.asyncio
async def test_async_post_call_failure_hook_ignores_none_exception_with_prompt_in_request_data():
    guard = StripOrphanToolChoiceGuard()
    assert guard.circuit_breaker.state == STATE_PRIMARY

    # Prompt containing 429 and resource_exhausted in request_data should NOT trip breaker when original_exception is None
    await guard.async_post_call_failure_hook(
        request_data={
            "model": "antigravity-flash-high",
            "messages": [{"role": "user", "content": "How does HTTP 429 rate limit or resource_exhausted work?"}],
        },
        original_exception=None,
        user_api_key_dict={},
    )
    assert guard.circuit_breaker.state == STATE_PRIMARY


@pytest.mark.asyncio
async def test_async_post_call_failure_hook_ignores_remote_model_failures():
    guard = StripOrphanToolChoiceGuard()
    assert guard.circuit_breaker.state == STATE_PRIMARY

    # Failure on remote model should not trip local circuit breaker
    await guard.async_post_call_failure_hook(
        request_data={"model": "antigravity-remote-flash-high"},
        original_exception=Exception("HTTP 429 Too Many Requests"),
        user_api_key_dict={},
    )
    assert guard.circuit_breaker.state == STATE_PRIMARY


def test_circuit_breaker_record_success_ignores_remote_model():
    cb = AntigravityCircuitBreaker()
    cb.trigger_failover()
    cb._state = STATE_PROBING
    assert cb.state == STATE_PROBING

    # Success on remote model should NOT close probing
    res = cb.record_success(model="antigravity-remote-flash-high")
    assert res is False
    assert cb.state == STATE_PROBING

    # Success on primary model SHOULD close probing
    res = cb.record_success(model="antigravity-flash-high")
    assert res is True
    assert cb.state == STATE_PRIMARY


def test_ranked_tuple_sort_with_dict_does_not_raise_type_error():
    # Verify that tuples with identical score and label sort by key=lambda x: x[:4] without comparing dicts
    cfg1 = {"modelId": "m1", "modelOrAlias": {"model": "tgt1"}}
    cfg2 = {"modelId": "m2", "modelOrAlias": {"model": "tgt2"}}
    ranked = [
        (1, 1, 0, "gemini", cfg1),
        (1, 1, 0, "gemini", cfg2),
    ]
    ranked.sort(reverse=True, key=lambda x: x[:4])
    assert ranked[0][-1] in (cfg1, cfg2)


def test_is_failover_error_detects_502_bad_gateway():
    class DummyHTTPError(Exception):
        def __init__(self, msg: str, status_code: int):
            super().__init__(msg)
            self.status_code = status_code

    # status_code attribute
    assert is_failover_error(DummyHTTPError("Bad Gateway", 502)) is True
    assert is_exhaustion_error(DummyHTTPError("Bad Gateway", 502)) is True

    # LiteLLM BadGatewayError
    bad_gw = litellm.BadGatewayError("Bad Gateway from upstream", "antigravity-flash", "openai")
    assert is_failover_error(bad_gw) is True
    assert is_exhaustion_error(bad_gw) is True

    # String representations
    assert is_failover_error(Exception("HTTP 502 Bad Gateway")) is True
    assert is_exhaustion_error(Exception("HTTP 502 Bad Gateway")) is True
    assert is_failover_error("Client error: 502 Bad Gateway") is True
    assert is_failover_error("status_code: 502") is True
    assert is_failover_error(Exception("Bad Gateway: connection refused")) is True
    assert is_failover_error(Exception("502 Server Error: Bad Gateway")) is True

    # Dict structures
    assert is_failover_error({"status_code": 502}) is True
    assert is_failover_error({"error_code": 502, "message": "bad gateway"}) is True
    assert is_failover_error({"code": 502}) is True
    assert is_failover_error({"status": "bad_gateway"}) is True
    assert is_failover_error({"type": "bad_gateway"}) is True
    assert is_failover_error({
        "error": {
            "message": "Upstream Antigravity language server returned empty response",
            "type": "bad_gateway",
            "code": 502,
        }
    }) is True
    assert is_failover_error('{"error": {"code": 502, "message": "Bad Gateway"}}') is True


def test_is_failover_error_detects_504_gateway_timeout():
    class DummyHTTPError(Exception):
        def __init__(self, msg: str, status_code: int):
            super().__init__(msg)
            self.status_code = status_code

    # status_code attribute
    assert is_failover_error(DummyHTTPError("Gateway Timeout", 504)) is True
    assert is_exhaustion_error(DummyHTTPError("Gateway Timeout", 504)) is True

    # String representations
    assert is_failover_error(Exception("HTTP 504 Gateway Timeout")) is True
    assert is_exhaustion_error(Exception("HTTP 504 Gateway Timeout")) is True
    assert is_failover_error("Client error: 504 Gateway Timeout") is True
    assert is_failover_error("status_code: 504") is True
    assert is_failover_error(Exception("Gateway Timeout: upstream language server did not respond in time")) is True
    assert is_failover_error(Exception("504 Server Error: Gateway Time-out")) is True

    # Dict structures
    assert is_failover_error({"status_code": 504}) is True
    assert is_failover_error({"error_code": 504, "message": "gateway timeout"}) is True
    assert is_failover_error({"code": 504}) is True
    assert is_failover_error({"status": "gateway_timeout"}) is True
    assert is_failover_error({"type": "gateway_timeout"}) is True
    assert is_failover_error({
        "error": {
            "message": "Gateway Timeout: upstream language server did not respond in time",
            "type": "gateway_timeout",
            "code": 504,
        }
    }) is True
    assert is_failover_error('{"error": {"code": 504, "message": "Gateway Timeout"}}') is True


def test_is_failover_error_rejects_benign_502_504_numbers():
    assert is_failover_error(Exception("Connection failed to 127.0.0.1:5020")) is False
    assert is_failover_error(Exception("Connection failed to 127.0.0.1:5040")) is False
    assert is_failover_error(Exception("Processed 502 tokens in 1.2s")) is False
    assert is_failover_error(Exception("Processed 504 tokens in 1.2s")) is False
    assert is_failover_error(Exception("SyntaxError on line 502")) is False
    assert is_failover_error(Exception("SyntaxError on line 504")) is False
    assert is_failover_error({"prompt": "Can you explain HTTP status code 502?"}) is False
    assert is_failover_error({"prompt": "Can you explain HTTP status code 504?"}) is False
    assert is_failover_error({"content": "Explain 502 bad gateway error"}) is False
    assert is_failover_error(None) is False
    assert is_failover_error("") is False


def test_circuit_breaker_record_failure_502_504_transitions_to_failover():
    class DummyHTTPError(Exception):
        def __init__(self, msg: str, status_code: int):
            super().__init__(msg)
            self.status_code = status_code

    cb = AntigravityCircuitBreaker()
    assert cb.state == STATE_PRIMARY

    # 502 failure
    res_502 = cb.record_failure(DummyHTTPError("Bad Gateway", 502))
    assert res_502 is True
    assert cb.state == STATE_FAILOVER
    assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-remote-flash-high"

    # Reset
    cb.reset()
    assert cb.state == STATE_PRIMARY

    # 504 failure
    res_504 = cb.record_failure(DummyHTTPError("Gateway Timeout", 504))
    assert res_504 is True
    assert cb.state == STATE_FAILOVER
    assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-remote-flash-high"


@pytest.mark.asyncio
async def test_guard_integration_post_call_failure_502_triggers_circuit_breaker():
    guard = StripOrphanToolChoiceGuard()
    assert guard.circuit_breaker.state == STATE_PRIMARY

    exc_502 = litellm.BadGatewayError("Bad Gateway", "antigravity-flash", "openai")
    await guard.async_post_call_failure_hook(
        request_data={"model": "antigravity-flash-high"},
        original_exception=exc_502,
        user_api_key_dict={},
    )
    assert guard.circuit_breaker.state == STATE_FAILOVER

    # Verify that subsequent pre-call requests are routed to remote deployment immediately
    payload = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "hello"}],
    }
    routed = await guard.async_pre_call_hook(
        user_api_key_dict={}, cache=None, data=payload, call_type="anthropic_messages"
    )
    assert routed["model"] == "antigravity-remote-flash-high"


@pytest.mark.asyncio
async def test_guard_integration_post_call_failure_504_triggers_circuit_breaker():
    guard = StripOrphanToolChoiceGuard()
    assert guard.circuit_breaker.state == STATE_PRIMARY

    exc_504 = Exception("HTTP 504 Gateway Timeout: upstream language server did not respond")
    await guard.async_post_call_failure_hook(
        request_data={"model": "antigravity-flash-high"},
        original_exception=exc_504,
        user_api_key_dict={},
    )
    assert guard.circuit_breaker.state == STATE_FAILOVER

    # Verify that subsequent pre-call requests are routed to remote deployment immediately
    payload = {
        "model": "antigravity-flash-low",
        "messages": [{"role": "user", "content": "hello"}],
    }
    routed = await guard.async_pre_call_hook(
        user_api_key_dict={}, cache=None, data=payload, call_type="anthropic_messages"
    )
    assert routed["model"] == "antigravity-remote-flash-low"


@pytest.mark.asyncio
async def test_guard_integration_post_call_failure_ignores_remote_502_504():
    guard = StripOrphanToolChoiceGuard()
    assert guard.circuit_breaker.state == STATE_PRIMARY

    # Failures on remote models should NOT trip the local breaker
    exc_502 = litellm.BadGatewayError("Bad Gateway", "antigravity-remote-flash-high", "openai")
    await guard.async_post_call_failure_hook(
        request_data={"model": "antigravity-remote-flash-high"},
        original_exception=exc_502,
        user_api_key_dict={},
    )
    assert guard.circuit_breaker.state == STATE_PRIMARY

    exc_504 = Exception("HTTP 504 Gateway Timeout")
    await guard.async_post_call_failure_hook(
        request_data={"model": "antigravity-remote-flash-high"},
        original_exception=exc_504,
        user_api_key_dict={},
    )
    assert guard.circuit_breaker.state == STATE_PRIMARY


# ============================================================================ #
# 9. Regression Locking: Benign Filter Failover Precision Tests                #
# ============================================================================ #

class TestFailoverBenignFilterRegression:
    """Regression-locking tests for legitimate gateway/rate-limit failover errors.

    Verifies that error strings containing host IP:port, traceback line numbers,
    or token statistics are NOT falsely suppressed by benign false-positive filters.
    """

    @pytest.mark.parametrize(
        "failover_string",
        [
            "HTTPConnectionPool(host=127.0.0.1:5596): 502 Bad Gateway",
            "Upstream proxy failure at 192.168.1.100:8000: 502 Bad Gateway",
            "502 Bad Gateway from 10.0.0.15:3000",
            "HTTPConnectionPool(host='127.0.0.1', port=5596): Max retries exceeded (Caused by ResponseError('502 Bad Gateway'))",
        ],
    )
    def test_failover_502_with_host_ip_port_evaluates_true(self, failover_string: str) -> None:
        """Legitimate 502 Bad Gateway with host/port must evaluate to True."""
        assert is_failover_error(failover_string) is True
        assert is_failover_error(Exception(failover_string)) is True
        assert is_exhaustion_error(failover_string) is True

    @pytest.mark.parametrize(
        "failover_string",
        [
            "Anthropic error: 429 Too Many Requests - prompt_tokens: 150, completion_tokens: 25",
            "Rate limit: 429 Too Many Requests (prompt_tokens: 150)",
            "Rate limit reached: 429 Too Many Requests (token_count: 4000)",
            "Error: 429 quota exceeded: total_tokens=15000",
        ],
    )
    def test_failover_429_with_token_metadata_evaluates_true(self, failover_string: str) -> None:
        """Legitimate 429 Too Many Requests with token metadata must evaluate to True."""
        assert is_failover_error(failover_string) is True
        assert is_failover_error(Exception(failover_string)) is True
        assert is_exhaustion_error(failover_string) is True

    @pytest.mark.parametrize(
        "failover_string",
        [
            "Gateway failure at line 42: 504 Gateway Timeout",
            "Exception at line 105: 504 Gateway Time-out",
            "Gateway Timeout at line 7: upstream server did not respond",
        ],
    )
    def test_failover_504_with_line_numbers_evaluates_true(self, failover_string: str) -> None:
        """Legitimate 504 Gateway Timeout with line numbers must evaluate to True."""
        assert is_failover_error(failover_string) is True
        assert is_failover_error(Exception(failover_string)) is True
        assert is_exhaustion_error(failover_string) is True

    @pytest.mark.parametrize(
        "failover_string",
        [
            "Canopy error: 402 Payment Required (consumed 50 tokens)",
            "Canopy error: status 402 (consumed 50 tokens)",
            "Canopy error: 402 payment required - proxy at 127.0.0.1:5596",
        ],
    )
    def test_canopy_402_with_token_or_host_evaluates_true(self, failover_string: str) -> None:
        """Canopy 402 payment exhaustion with token counts or proxy host must evaluate to True."""
        assert is_failover_error(failover_string) is True
        assert is_failover_error(Exception(failover_string)) is True
        assert is_exhaustion_error(failover_string) is True

    @pytest.mark.parametrize(
        "benign_string",
        [
            "token_count: 402",
            "tokens: 402",
            "prompt_tokens: 402",
            "completion_tokens: 402",
            "total_tokens: 402",
            "Connected to 10.0.0.1:402",
            "Connection to 10.0.0.1:402 failed",
            "Connected to 192.168.1.50:402",
            "explanation of 402",
            "user prompt asking about 402",
            "HTTP 402 error explanation in message body",
            "Connection failed to 127.0.0.1:5020",
            "Connection failed to 127.0.0.1:5040",
            "Processed 502 tokens in 1.2s",
            "Processed 504 tokens in 1.2s",
            "SyntaxError on line 502",
            "SyntaxError on line 504",
            "SyntaxError on line 402",
            "Port 402 is closed",
            "found 402 results in database query",
            "found 502 results in database query",
        ],
    )
    def test_benign_strings_without_status_codes_evaluate_false(self, benign_string: str) -> None:
        """Benign mentions of numbers without HTTP status/error context must evaluate to False."""
        assert is_failover_error(benign_string) is False
        assert is_failover_error(Exception(benign_string)) is False
        assert is_exhaustion_error(benign_string) is False

    def test_circuit_breaker_trips_on_proxy_host_502(self) -> None:
        """Circuit breaker must trip to FAILOVER when encountering 502 on local proxy port."""
        cb = AntigravityCircuitBreaker()
        assert cb.state == STATE_PRIMARY

        exc = Exception("HTTPConnectionPool(host=127.0.0.1:5596): 502 Bad Gateway")
        res = cb.record_failure(exc)
        assert res is True
        assert cb.state == STATE_FAILOVER
        assert cb.get_target_for_model("antigravity-flash-high") == "antigravity-remote-flash-high"


# ============================================================================ #
# Milestone 3 (Phase 3): Hook Refactoring, Rotation & Pause Guard Tests        #
# ============================================================================ #

import httpx
import re
import yaml
from service.agentic.endpoint_abstraction.wallet_manager import WalletManager
from service.agentic.endpoint_abstraction.pause_manager import PauseManager


@pytest.mark.asyncio
async def test_deployment_hook_injects_wallet_manager_active_key(tmp_path: Path):
    """Verify async_pre_call_deployment_hook dynamically injects active key from WalletManager."""
    keystore_path = tmp_path / "unstoppable_wallets.json"
    wm = WalletManager(keystore_path=keystore_path, auto_create=True)
    wm.add_wallet("csk_dynamic_test_key_11111111", wallet_id="w1", set_active=True)

    guard = StripOrphanToolChoiceGuard(wallet_manager=wm)
    kwargs = {
        "model": "deepseek-v4-pro",
        "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic",
        "stream": False,
    }
    res = await guard.async_pre_call_deployment_hook(kwargs)

    assert res["api_key"] == "csk_dynamic_test_key_11111111"
    assert res["extra_headers"]["Authorization"] == "Bearer csk_dynamic_test_key_11111111"
    assert res["stream"] is True
    assert res["extra_headers"]["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_deployment_hook_fast_fails_when_pause_manager_is_paused(tmp_path: Path):
    """Verify async_pre_call_deployment_hook raises 402 HTTPException when PauseManager is paused."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    pm.pause(
        reason="All wallets exhausted (personal_wallet_empty)",
        correlation_id="req_canopy_test_123",
    )

    guard = StripOrphanToolChoiceGuard(pause_manager=pm)
    kwargs = {
        "model": "deepseek-v4-pro",
        "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic",
    }
    with pytest.raises(Exception) as exc_info:
        await guard.async_pre_call_deployment_hook(kwargs)

    assert getattr(exc_info.value, "status_code", None) == 402
    detail = getattr(exc_info.value, "detail", {})
    assert "error" in detail
    assert detail["error"]["code"] == 402
    assert "Unstoppable AI Resource Depleted" in detail["error"]["message"]
    assert detail["error"]["canopy_correlation_id"] == "req_canopy_test_123"


@pytest.mark.asyncio
async def test_deployment_hook_does_not_fast_fail_for_antigravity_models_when_paused(tmp_path: Path):
    """Verify pause lock on Unstoppable endpoints does not block standard Antigravity models."""
    lock_path = tmp_path / "unstoppable_pause.lock"
    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="All wallets exhausted", correlation_id="req_canopy_test_456")

    guard = StripOrphanToolChoiceGuard(pause_manager=pm)
    kwargs = {
        "model": "antigravity-flash",
        "api_base": "http://127.0.0.1:8788/v1",
        "tools": None,
    }
    res = await guard.async_pre_call_deployment_hook(kwargs)
    assert res["model"] == "antigravity-flash"


@pytest.mark.asyncio
async def test_post_call_failure_rotates_wallet_on_canopy_402(tmp_path: Path):
    """Verify post_call_failure hook rotates active wallet when Canopy 402 is classified."""
    keystore_path = tmp_path / "unstoppable_wallets.json"
    wm = WalletManager(keystore_path=keystore_path, auto_create=True)
    wm.add_wallet("csk_key_alpha_11111111", wallet_id="w1", wallet_type="personal", set_active=True)
    wm.add_wallet("csk_key_beta_22222222", wallet_id="w2", wallet_type="org")

    assert wm.active_wallet_id == "w1"

    guard = StripOrphanToolChoiceGuard(wallet_manager=wm)
    resp = httpx.Response(
        status_code=402,
        json={"error": {"reason": "personal_wallet_empty", "canopy_proxy": "CANOPY_AI_CREDITS_PROXY"}},
        headers={"x-canopy-correlation-id": "req_canopy_rotate_789"},
        request=httpx.Request("POST", "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic/v1/messages"),
    )
    exc_402 = httpx.HTTPStatusError(
        message="402 Payment Required: personal_wallet_empty",
        request=resp.request,
        response=resp,
    )

    await guard.async_post_call_failure_hook(
        request_data={"model": "deepseek-v4-pro", "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic"},
        original_exception=exc_402,
        user_api_key_dict={},
    )

    # Active wallet should now be w2
    assert wm.active_wallet_id == "w2"
    assert wm.get_active_key() == "csk_key_beta_22222222"


@pytest.mark.asyncio
async def test_post_call_failure_triggers_pause_when_all_wallets_exhausted(tmp_path: Path):
    """Verify post_call_failure hook triggers PauseManager.pause when all wallets exhausted."""
    keystore_path = tmp_path / "unstoppable_wallets.json"
    lock_path = tmp_path / "unstoppable_pause.lock"

    wm = WalletManager(keystore_path=keystore_path, auto_create=True)
    wm.add_wallet("csk_key_solo_11111111", wallet_id="w_solo", set_active=True)

    pm = PauseManager(lock_path=lock_path)
    assert pm.is_paused() is False

    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)
    resp = httpx.Response(
        status_code=402,
        json={"error": {"reason": "personal_wallet_empty", "canopy_proxy": "CANOPY_AI_CREDITS_PROXY"}},
        headers={"x-canopy-correlation-id": "req_canopy_final_999"},
        request=httpx.Request("POST", "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic/v1/messages"),
    )
    exc_402 = httpx.HTTPStatusError(
        message="402 Payment Required: personal_wallet_empty",
        request=resp.request,
        response=resp,
    )

    await guard.async_post_call_failure_hook(
        request_data={"model": "deepseek-v4-pro", "api_base": "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic"},
        original_exception=exc_402,
        user_api_key_dict={},
    )

    # Keystore has no active wallet, PauseManager is now paused
    assert wm.has_active_wallet() is False
    assert pm.is_paused() is True
    pause_state = pm.get_pause_state()
    assert pause_state is not None
    assert pause_state.correlation_id == "req_canopy_final_999"
    assert "All wallets exhausted" in pause_state.reason


@pytest.mark.asyncio
async def test_pre_call_hook_injects_wallet_manager_active_key(tmp_path: Path):
    """Verify async_pre_call_hook routes opus to deepseek-v4-pro with dynamic key."""
    keystore_path = tmp_path / "unstoppable_wallets.json"
    wm = WalletManager(keystore_path=keystore_path, auto_create=True)
    wm.add_wallet("csk_pre_call_dynamic_999", wallet_id="w_pre", set_active=True)

    guard = StripOrphanToolChoiceGuard(wallet_manager=wm)
    data = {
        "model": "claude-opus-5.5",
        "stream": False,
    }
    res = await guard.async_pre_call_hook(
        user_api_key_dict={},
        cache=None,
        data=data,
        call_type="acompletion",
    )
    assert res["model"] == "deepseek-v4-pro"
    assert res["stream"] is True
    assert res["api_key"] == "csk_pre_call_dynamic_999"
    assert res["extra_headers"]["Authorization"] == "Bearer csk_pre_call_dynamic_999"
    assert res["extra_headers"]["anthropic-version"] == "2023-06-01"


def test_config_yaml_has_router_fallbacks_for_unstoppable_models():
    """Verify router_settings.fallbacks in config/config.yaml maps deepseek and glm to antigravity-pro."""
    repo_config = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
    with open(repo_config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    fallbacks = cfg.get("router_settings", {}).get("fallbacks", [])
    fallback_map = {}
    for item in fallbacks:
        if isinstance(item, dict):
            fallback_map.update(item)

    assert "deepseek-v4-pro" in fallback_map
    assert "antigravity-pro" in fallback_map["deepseek-v4-pro"]
    assert "glm-5.3-flash" in fallback_map
    assert "antigravity-pro" in fallback_map["glm-5.3-flash"]


def test_zero_forbidden_references_in_tool_choice_guard():
    """Verify zero forbidden terms in tool_choice_guard.py."""
    forbidden = "".join(["black", "hole"])
    guard_source = Path(__file__).resolve().parent.parent / "tool_choice_guard.py"
    pattern = re.compile(forbidden, re.IGNORECASE)
    assert not pattern.search(guard_source.read_text(encoding="utf-8")), "Forbidden term found in tool_choice_guard.py"







