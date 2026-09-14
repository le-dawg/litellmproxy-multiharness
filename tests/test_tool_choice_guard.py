import asyncio
import pytest
import sys
from pathlib import Path

# Add repository root and ~/.litellm to python path
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(Path.home() / ".litellm"))

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
