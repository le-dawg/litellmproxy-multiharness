import asyncio
import pytest
import sys
from pathlib import Path

# Add ~/.litellm and repository root to python path (repo_root takes priority)
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path.home() / ".litellm"))
sys.path.insert(0, str(repo_root))

from tool_choice_guard import StripOrphanToolChoiceGuard


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


