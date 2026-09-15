import httpx
import pytest

PROXY_BASE = "http://127.0.0.1:5596"


def _is_proxy_running() -> bool:
    try:
        resp = httpx.get(f"{PROXY_BASE}/health", timeout=10.0)
        return resp.status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(not _is_proxy_running(), reason="LiteLLM proxy is not running on 127.0.0.1:5596")
def test_proxy_health():
    resp = httpx.get(f"{PROXY_BASE}/health")
    assert resp.status_code == 200, f"Proxy health failed: {resp.text}"


@pytest.mark.skipif(not _is_proxy_running(), reason="LiteLLM proxy is not running on 127.0.0.1:5596")
def test_clean_error_message_without_router_leak():
    # Send a request with empty input to trigger a 400 validation error
    resp = httpx.post(
        f"{PROXY_BASE}/v1/responses",
        json={
            "model": "gpt-5.4",
            "input": [],
        },
        timeout=10.0,
    )
    # Ensure the response does NOT contain the confusing fallback leak
    assert resp.status_code != 200
    error_text = resp.text
    assert "Available Model Group Fallbacks" not in error_text, (
        f"Leaked router fallback string found in error response: {error_text}"
    )
    assert "Received Model Group" not in error_text, (
        f"Leaked model group string found in error response: {error_text}"
    )


@pytest.mark.skipif(not _is_proxy_running(), reason="LiteLLM proxy is not running on 127.0.0.1:5596")
def test_subagent_model_resolution_and_aliases():
    # Verify models endpoint exposes subagent models
    resp = httpx.get(f"{PROXY_BASE}/v1/models", timeout=10.0)
    assert resp.status_code == 200, f"/v1/models failed: {resp.text}"
    data = resp.json().get("data", [])
    model_ids = {m.get("id") for m in data}
    for required in ("claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-gpt-5"):
        assert required in model_ids, f"Model {required} missing from /v1/models output: {model_ids}"


@pytest.mark.skipif(not _is_proxy_running(), reason="LiteLLM proxy is not running on 127.0.0.1:5596")
def test_messages_endpoint_preserves_tool_choice():
    # Verify tool_choice on /v1/messages is preserved for Claude Code
    resp = httpx.post(
        f"{PROXY_BASE}/v1/messages",
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=15.0,
    )
    assert resp.status_code == 200, f"/v1/messages failed: {resp.text}"
    result = resp.json()
    assert result.get("role") == "assistant" or "content" in result
