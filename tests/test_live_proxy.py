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
