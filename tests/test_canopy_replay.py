"""End-to-End Canopy Replay & Multi-Wallet Failover Test Suite.

Scope:
- Phase 5 Task 5.1: Replay historical 402 session failure against proxy verifying clean failover to Wallet 2.
- Phase 5 Task 5.2: Verify all-wallets-exhausted state produces enhanced 402 and activates probe daemon.

Test Runner:
    uv run pytest tests/test_canopy_replay.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from service.agentic.endpoint_abstraction.canopy_classifier import CanopyClassifier
from service.agentic.endpoint_abstraction.error_enricher import (
    DEFAULT_DEPLETED_MESSAGE,
    DEFAULT_RESUME_INSTRUCTION,
    CanopyResourceDepletedError,
    ErrorEnricher,
)
from service.agentic.endpoint_abstraction.pause_manager import PauseManager, PauseState
from service.agentic.endpoint_abstraction.wallet_manager import Wallet, WalletManager
from tool_choice_guard import StripOrphanToolChoiceGuard, is_unstoppable_model


# ============================================================================ #
# Dynamic Mock Canopy Server (127.0.0.1:0)                                     #
# ============================================================================ #

class DynamicCanopyHandler(BaseHTTPRequestHandler):
    """Dynamic mock handler discriminating requests by Authorization header."""

    @property
    def mock_server(self) -> Optional["DynamicCanopyServer"]:
        return getattr(self.server, "mock_server", None)

    def do_POST(self) -> None:
        auth = self.headers.get("Authorization", "")
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b""
        body: Dict[str, Any] = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = {"raw": raw.decode("utf-8", "replace")}

        req_record = {
            "method": "POST",
            "path": self.path,
            "headers": dict(self.headers),
            "auth": auth,
            "body": body,
            "raw": raw,
            "timestamp": time.time(),
        }

        mock = self.mock_server
        if mock is not None:
            mock.record_request(req_record)
            mode = mock.mode
        else:
            mode = "key_discriminated"

        if mode == "all_402":
            self._send_canopy_402("req_canopy_all_exhausted")
            return

        if mode == "all_200":
            self._send_canopy_200_sse("req_canopy_all_ok")
            return

        # Key-discriminated behavior
        if "csk_live_alpha" in auth:
            # Wallet 1 returns Canopy 402 personal_wallet_empty
            self._send_canopy_402("req_canopy_replay_turn_01_alpha")
        elif "csk_live_backup" in auth:
            # Wallet 2 returns 200 OK SSE stream
            self._send_canopy_200_sse("req_canopy_turn_ok_backup")
        else:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "unauthorized_wallet_key"}')

    def _send_canopy_402(self, correlation_id: str) -> None:
        payload = {
            "error": {
                "canopy_proxy": "CANOPY_AI_CREDITS_PROXY",
                "code": "no_billing_source",
                "correlation_id": correlation_id,
                "reason": "personal_wallet_empty",
                "message": "AI Credits are unavailable for this request: personal_wallet_empty",
            }
        }
        resp_bytes = json.dumps(payload).encode("utf-8")
        self.send_response(402)
        self.send_header("Content-Type", "application/json")
        self.send_header("x-canopy-correlation-id", correlation_id)
        self.send_header("Content-Length", str(len(resp_bytes)))
        self.end_headers()
        self.wfile.write(resp_bytes)

    def _send_canopy_200_sse(self, correlation_id: str) -> None:
        sse_content = (
            b"event: message_start\n"
            b'data: {"type": "message_start", "message": {"id": "msg_replay_ok", "role": "assistant"}}\n\n'
            b"event: content_block_delta\n"
            b'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Turn completed with Wallet 2!"}}\n\n'
            b"event: message_stop\n"
            b'data: {"type": "message_stop"}\n\n'
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("x-canopy-correlation-id", correlation_id)
        self.send_header("Content-Length", str(len(sse_content)))
        self.end_headers()
        self.wfile.write(sse_content)

    def log_message(self, *args: Any) -> None:
        """Suppress standard logging in tests."""
        pass


class DynamicCanopyServer:
    """Ephemeral mock server bound to dynamic port on 127.0.0.1:0."""

    def __init__(self, mode: str = "key_discriminated") -> None:
        self.mode = mode
        self.recorded_requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DynamicCanopyHandler)
        self.server.mock_server = self  # type: ignore[attr-defined]
        self.host, self.port = self.server.server_address
        self.url = f"http://{self.host}:{self.port}/api/v1/llm-proxy/anthropic/v1/messages"
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "DynamicCanopyServer":
        if self._thread is None:
            self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self.server.shutdown()
            self.server.server_close()
            self._thread.join(timeout=3.0)
            self._thread = None

    def __enter__(self) -> "DynamicCanopyServer":
        return self.start()

    def __exit__(self, *args: Any) -> None:
        self.stop()

    def record_request(self, req: Dict[str, Any]) -> None:
        with self._lock:
            self.recorded_requests.append(req)

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self.mode = mode

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self.recorded_requests)

    @property
    def last_request(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.recorded_requests[-1] if self.recorded_requests else None


# ============================================================================ #
# Fixtures & Environment Setup                                                 #
# ============================================================================ #

@pytest.fixture
def isolated_env(tmp_path: Path) -> Tuple[Path, Path]:
    """Provide isolated paths for keystore and pause lockfiles."""
    litellm_dir = tmp_path / ".litellm"
    litellm_dir.mkdir(parents=True, exist_ok=True)
    wallets_path = litellm_dir / "unstoppable_wallets.json"
    lock_path = litellm_dir / "unstoppable_pause.lock"
    return wallets_path, lock_path


@pytest.fixture
def mock_canopy():
    """Run an isolated DynamicCanopyServer on dynamic port 127.0.0.1:0."""
    server = DynamicCanopyServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def two_wallet_pool(isolated_env: Tuple[Path, Path]) -> Tuple[WalletManager, PauseManager]:
    """Initialize a dual-wallet keystore with wallet_primary and wallet_backup_1."""
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {
                "id": "wallet_primary",
                "key": "csk_live_alpha_111111111111111111111111",
                "type": "personal",
                "state": "active",
                "last_exhausted": None,
            },
            {
                "id": "wallet_backup_1",
                "key": "csk_live_backup_222222222222222222222222",
                "type": "org",
                "state": "active",
                "last_exhausted": None,
            },
        ],
        "active_wallet_id": "wallet_primary",
        "updated_at": 1793836800.0,
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, stat.S_IRUSR | stat.S_IWUSR)

    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    return wm, pm


@pytest.fixture(autouse=True)
def isolate_global_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure host machine ~/.litellm/.unstoppable_key does not leak into hermetic replay tests."""
    monkeypatch.setattr("tool_choice_guard.get_unstoppable_code_key", lambda: None)


# ============================================================================ #
# Replay Dispatch Helper                                                       #
# ============================================================================ #

async def execute_turn_with_replay(
    guard: StripOrphanToolChoiceGuard,
    mock_server: DynamicCanopyServer,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    model: str = "deepseek-v4-pro",
    max_retries: int = 5,
) -> httpx.Response:
    """Simulate the LiteLLM proxy dispatch cycle with hooks, failure failover, and retry."""
    kwargs: Dict[str, Any] = {
        "model": model,
        "api_base": mock_server.url,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools

    resp: Optional[httpx.Response] = None

    for _ in range(max_retries):
        # 1. Pre-call deployment hook
        await guard.async_pre_call_deployment_hook(kwargs)

        # 2. Dispatch HTTP call to upstream
        headers = dict(kwargs.get("extra_headers", {}))
        payload = {
            "model": kwargs.get("model"),
            "messages": kwargs.get("messages"),
            "stream": kwargs.get("stream", True),
            "max_tokens": 100,
        }
        if "tools" in kwargs:
            payload["tools"] = kwargs["tools"]

        async with httpx.AsyncClient() as client:
            resp = await client.post(mock_server.url, json=payload, headers=headers, timeout=10.0)

        # 3. Handle response / failure
        if resp.status_code == 200:
            await guard.async_post_call_success_hook(kwargs, {}, resp)
            return resp

        if resp.status_code == 402:
            exc = httpx.HTTPStatusError(
                f"Client error '402 Payment Required' for url '{mock_server.url}'",
                request=resp.request,
                response=resp,
            )
            await guard.async_post_call_failure_hook(kwargs, exc, {})

            # If all wallets in keystore are now exhausted, stop retrying
            if guard.wallet_manager and not guard.wallet_manager.has_active_wallet():
                return resp
            # Otherwise loop to retry with newly rotated active wallet
        else:
            return resp

    assert resp is not None
    return resp


# ============================================================================ #
# Suite 1: Task 5.1 Historical 402 Replay & Clean Failover to Wallet 2        #
# ============================================================================ #

@pytest.mark.asyncio
async def test_5_1_historical_402_multi_turn_replay_and_failover_to_wallet_2(
    two_wallet_pool: Tuple[WalletManager, PauseManager],
    mock_canopy: DynamicCanopyServer,
):
    """Replay historical multi-turn conversation turn encountering 402 and verify clean failover to Wallet 2."""
    wm, pm = two_wallet_pool
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    assert wm.active_wallet_id == "wallet_primary"
    assert wm.get_active_key() == "csk_live_alpha_111111111111111111111111"
    assert pm.is_paused() is False

    # Turn 1: Initial user query
    turn1_messages = [{"role": "user", "content": "Hello, please list the directory files."}]
    resp1 = await execute_turn_with_replay(guard, mock_canopy, turn1_messages)

    # Assert Turn 1 succeeded via failover retry
    assert resp1.status_code == 200
    assert "Turn completed with Wallet 2!" in resp1.text
    assert resp1.headers.get("x-canopy-correlation-id") == "req_canopy_turn_ok_backup"

    # Assert server recorded exactly 2 requests for Turn 1
    assert mock_canopy.request_count == 2
    first_req = mock_canopy.recorded_requests[0]
    second_req = mock_canopy.recorded_requests[1]

    # Verify first request used Wallet 1 and enforced stream: true
    assert "csk_live_alpha" in first_req["auth"]
    assert first_req["body"].get("stream") is True

    # Verify second request used Wallet 2 and enforced stream: true
    assert "csk_live_backup" in second_req["auth"]
    assert second_req["body"].get("stream") is True

    # Assert keystore rotated active wallet to Wallet 2 and system remains unpaused
    assert wm.active_wallet_id == "wallet_backup_1"
    assert wm.get_active_key() == "csk_live_backup_222222222222222222222222"
    assert pm.is_paused() is False


@pytest.mark.asyncio
async def test_5_1_masked_api_connection_error_multi_turn_replay(
    two_wallet_pool: Tuple[WalletManager, PauseManager],
    mock_canopy: DynamicCanopyServer,
):
    """Verify failover to Wallet 2 occurs when 402 is masked inside an APIConnectionError string."""
    wm, pm = two_wallet_pool
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    # Simulate masked error containing verbatim Canopy 402 payload
    masked_error_str = (
        "litellm.exceptions.APIConnectionError: AnthropicException - "
        "b'{\"error\":{\"canopy_proxy\":\"CANOPY_AI_CREDITS_PROXY\",\"code\":\"no_billing_source\","
        "\"correlation_id\":\"corr_masked_replay_01\",\"reason\":\"personal_wallet_empty\","
        "\"message\":\"AI Credits are unavailable for this request.\"}}'"
    )

    class SimulatedAPIConnectionError(Exception):
        pass

    exc = SimulatedAPIConnectionError(masked_error_str)

    # Verify CanopyClassifier identifies it
    assert CanopyClassifier.is_canopy_exhaustion(exc) is True
    assert CanopyClassifier.extract_correlation_id(exc) == "corr_masked_replay_01"
    assert CanopyClassifier.extract_exhaustion_reason(exc) == "personal_wallet_empty"

    request_data = {
        "model": "deepseek-v4-pro",
        "api_base": mock_canopy.url,
        "messages": [{"role": "user", "content": "masked test"}],
    }

    # Dispatch to post_call_failure_hook
    await guard.async_post_call_failure_hook(request_data, exc, {})

    # Verify active wallet rotated to Wallet 2
    assert wm.active_wallet_id == "wallet_backup_1"
    assert wm.get_active_key() == "csk_live_backup_222222222222222222222222"
    assert pm.is_paused() is False


@pytest.mark.asyncio
async def test_5_1_masked_http_status_error_multi_turn_replay(
    two_wallet_pool: Tuple[WalletManager, PauseManager],
    mock_canopy: DynamicCanopyServer,
):
    """Verify failover to Wallet 2 occurs when encountering httpx.HTTPStatusError with 402 status."""
    wm, pm = two_wallet_pool
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    req = httpx.Request("POST", mock_canopy.url)
    resp = httpx.Response(
        status_code=402,
        headers={"x-canopy-correlation-id": "req_status_err_123"},
        json={
            "error": {
                "canopy_proxy": "CANOPY_AI_CREDITS_PROXY",
                "code": "no_billing_source",
                "reason": "personal_wallet_empty",
                "message": "AI Credits depleted",
            }
        },
        request=req,
    )
    exc = httpx.HTTPStatusError("402 Payment Required", request=req, response=resp)

    request_data = {"model": "deepseek-v4-pro", "api_base": mock_canopy.url}
    await guard.async_post_call_failure_hook(request_data, exc, {})

    assert wm.active_wallet_id == "wallet_backup_1"
    assert pm.is_paused() is False


@pytest.mark.asyncio
async def test_5_1_multi_turn_tool_call_history_preserved_across_failover(
    two_wallet_pool: Tuple[WalletManager, PauseManager],
    mock_canopy: DynamicCanopyServer,
):
    """Verify tool call definitions and multi-turn message history are preserved across failover."""
    wm, pm = two_wallet_pool
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "Bash",
                "description": "Execute bash commands",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        }
    ]

    history = [
        {"role": "user", "content": "Run git status"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_git_status",
                    "type": "function",
                    "function": {"name": "Bash", "arguments": '{"command": "git status"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_git_status",
            "name": "Bash",
            "content": "On branch main, working tree clean",
        },
        {"role": "user", "content": "Now check git log"},
    ]

    resp = await execute_turn_with_replay(guard, mock_canopy, history, tools=tools)
    assert resp.status_code == 200

    # Ensure mock server received the complete history and tool definitions
    last_req = mock_canopy.last_request
    assert last_req is not None
    assert len(last_req["body"]["messages"]) == 4
    assert last_req["body"]["tools"] == tools
    assert "csk_live_backup" in last_req["auth"]


@pytest.mark.asyncio
async def test_5_1_atomic_keystore_state_persistence_on_disk(
    isolated_env: Tuple[Path, Path],
    two_wallet_pool: Tuple[WalletManager, PauseManager],
    mock_canopy: DynamicCanopyServer,
):
    """Verify keystore rotation updates unstoppable_wallets.json on disk atomically with 0600 mode."""
    wallets_path, lock_path = isolated_env
    wm, pm = two_wallet_pool
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    turn1 = [{"role": "user", "content": "Testing disk persistence"}]
    await execute_turn_with_replay(guard, mock_canopy, turn1)

    # Read the raw file directly from disk
    raw_disk_data = json.loads(wallets_path.read_text(encoding="utf-8"))
    assert raw_disk_data["active_wallet_id"] == "wallet_backup_1"

    primary = next(w for w in raw_disk_data["wallets"] if w["id"] == "wallet_primary")
    assert primary["state"] == "exhausted"
    assert primary["last_exhausted"] is not None

    backup = next(w for w in raw_disk_data["wallets"] if w["id"] == "wallet_backup_1")
    assert backup["state"] == "active"

    # Verify file mode is 0600
    file_mode = stat.S_IMODE(wallets_path.stat().st_mode)
    assert file_mode == 0o600

    # Instantiate a fresh WalletManager pointing to the same file
    fresh_wm = WalletManager(keystore_path=wallets_path)
    assert fresh_wm.active_wallet_id == "wallet_backup_1"
    assert fresh_wm.get_active_key() == "csk_live_backup_222222222222222222222222"


@pytest.mark.asyncio
async def test_5_1_subsequent_turns_seamlessly_use_wallet_2(
    two_wallet_pool: Tuple[WalletManager, PauseManager],
    mock_canopy: DynamicCanopyServer,
):
    """Verify subsequent turns continue using Wallet 2 without re-triggering failover or 402."""
    wm, pm = two_wallet_pool
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    # Turn 1: Triggers failover
    turn1 = [{"role": "user", "content": "Turn 1"}]
    resp1 = await execute_turn_with_replay(guard, mock_canopy, turn1)
    assert resp1.status_code == 200
    assert mock_canopy.request_count == 2

    # Turn 2: Subsequent turn
    turn2 = [
        {"role": "user", "content": "Turn 1"},
        {"role": "assistant", "content": "Turn 1 reply"},
        {"role": "user", "content": "Turn 2 question"},
    ]
    resp2 = await execute_turn_with_replay(guard, mock_canopy, turn2)
    assert resp2.status_code == 200
    # Exactly 1 new request sent (no 402, total = 3)
    assert mock_canopy.request_count == 3
    assert "csk_live_backup" in mock_canopy.recorded_requests[2]["auth"]

    # Turn 3: Subsequent turn
    turn3 = [
        *turn2,
        {"role": "assistant", "content": "Turn 2 reply"},
        {"role": "user", "content": "Turn 3 question"},
    ]
    resp3 = await execute_turn_with_replay(guard, mock_canopy, turn3)
    assert resp3.status_code == 200
    # Exactly 1 new request sent (no 402, total = 4)
    assert mock_canopy.request_count == 4
    assert "csk_live_backup" in mock_canopy.recorded_requests[3]["auth"]


# ============================================================================ #
# Suite 2: Task 5.2 All-Wallets-Exhausted Backpressure & Fast-Fail Gate        #
# ============================================================================ #

@pytest.mark.asyncio
async def test_5_2_all_wallets_exhausted_cascade_triggers_pause_lock(
    isolated_env: Tuple[Path, Path],
    two_wallet_pool: Tuple[WalletManager, PauseManager],
    mock_canopy: DynamicCanopyServer,
):
    """When all wallets in pool are exhausted, pause lock is created and proxy enters paused state."""
    wallets_path, lock_path = isolated_env
    wm, pm = two_wallet_pool
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    # Configure server to reject all wallets with 402
    mock_canopy.set_mode("all_402")

    turn1 = [{"role": "user", "content": "Exhaust all wallets test"}]
    resp = await execute_turn_with_replay(guard, mock_canopy, turn1)
    assert resp.status_code == 402

    # Verify pause lock engaged
    assert pm.is_paused() is True
    assert lock_path.is_file()

    # Verify lock file schema matching Interface Contract 2
    lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert lock_data["paused"] is True
    assert isinstance(lock_data["paused_at"], (int, float))
    assert "All wallets exhausted" in lock_data["reason"]
    assert lock_data["correlation_id"] is not None

    # Verify lockfile mode is 0600
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_5_2_pre_exhausted_pool_creates_pause_lock_and_402(
    isolated_env: Tuple[Path, Path],
    mock_canopy: DynamicCanopyServer,
):
    """When pool starts with all wallets pre-exhausted, deployment hook creates lock and fast-fails."""
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {
                "id": "w1",
                "key": "csk_dead_1",
                "type": "personal",
                "state": "exhausted",
                "last_exhausted": time.time(),
            },
            {
                "id": "w2",
                "key": "csk_dead_2",
                "type": "org",
                "state": "exhausted",
                "last_exhausted": time.time(),
            },
        ],
        "active_wallet_id": None,
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)

    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    kwargs = {
        "model": "deepseek-v4-pro",
        "api_base": mock_canopy.url,
        "messages": [{"role": "user", "content": "ping"}],
    }

    with pytest.raises(Exception) as exc_info:
        await guard.async_pre_call_deployment_hook(kwargs)

    exc = exc_info.value
    assert getattr(exc, "status_code", None) == 402
    assert pm.is_paused() is True
    assert lock_path.is_file()
    assert mock_canopy.request_count == 0


@pytest.mark.asyncio
async def test_5_2_fast_fail_enhanced_402_matches_interface_contract_3(
    isolated_env: Tuple[Path, Path],
    mock_canopy: DynamicCanopyServer,
):
    """Verify fast-fail 402 matches exact Interface Contract 3 payload and headers."""
    wallets_path, lock_path = isolated_env
    pm = PauseManager(lock_path=lock_path)
    pm.pause(
        reason="All wallets exhausted (personal_wallet_empty)",
        correlation_id="req_canopy_contract3_test",
    )
    guard = StripOrphanToolChoiceGuard(pause_manager=pm)

    kwargs = {
        "model": "deepseek-v4-pro",
        "api_base": mock_canopy.url,
        "messages": [{"role": "user", "content": "fast fail contract probe"}],
    }

    with pytest.raises(Exception) as exc_info:
        await guard.async_pre_call_deployment_hook(kwargs)

    exc = exc_info.value
    assert getattr(exc, "status_code", None) == 402

    detail = getattr(exc, "detail", {})
    assert "error" in detail
    err = detail["error"]

    # Interface Contract 3 assertions
    assert err["message"] == DEFAULT_DEPLETED_MESSAGE
    assert err["type"] == "payment_required"
    assert err["code"] == 402
    assert err["canopy_correlation_id"] == "req_canopy_contract3_test"
    assert err["status"] == "paused"
    assert err["resume_instruction"] == DEFAULT_RESUME_INSTRUCTION

    headers = getattr(exc, "headers", {})
    assert headers.get("X-Canopy-Correlation-Id") == "req_canopy_contract3_test"
    assert headers.get("Content-Type") == "application/json"


@pytest.mark.asyncio
async def test_5_2_fast_fail_zero_egress_backpressure(
    isolated_env: Tuple[Path, Path],
    mock_canopy: DynamicCanopyServer,
):
    """Verify paused guard rejects requests instantly with zero upstream network egress."""
    wallets_path, lock_path = isolated_env
    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="Backpressure test", correlation_id="req_zero_egress")
    guard = StripOrphanToolChoiceGuard(pause_manager=pm)

    initial_requests = mock_canopy.request_count

    # Fire 5 consecutive requests
    for i in range(5):
        kwargs = {
            "model": "deepseek-v4-pro",
            "api_base": mock_canopy.url,
            "messages": [{"role": "user", "content": f"query {i}"}],
        }
        with pytest.raises(Exception) as exc_info:
            await guard.async_pre_call_deployment_hook(kwargs)
        assert getattr(exc_info.value, "status_code", None) == 402

    # Assert 0 upstream requests were made
    assert mock_canopy.request_count == initial_requests

    # Assert non-unstoppable models are NOT blocked by pause guard
    local_kwargs = {"model": "gpt-5.4", "messages": [{"role": "user", "content": "local model"}]}
    await guard.async_pre_call_deployment_hook(local_kwargs)
    assert local_kwargs.get("model") == "gpt-5.4"


def test_5_2_activates_probe_daemon_under_pid_lock(
    isolated_env: Tuple[Path, Path],
    tmp_path: Path,
):
    """Verify PauseManager spawns the background probe daemon and records PID in lockfile."""
    wallets_path, lock_path = isolated_env
    pm = PauseManager(lock_path=lock_path)
    pm.pause(reason="Daemon test", correlation_id="req_daemon_test")

    # Create a lightweight dummy probe script to safely test spawning without side effects
    probe_script = tmp_path / "test_probe.sh"
    probe_script.write_text(
        "#!/bin/bash\n"
        "trap 'exit 0' SIGTERM\n"
        "while true; do sleep 1; done\n",
        encoding="utf-8",
    )
    probe_script.chmod(0o755)

    pid = pm.ensure_probe_daemon_running(script_path=probe_script)
    assert pid is not None
    assert pid > 0

    try:
        # Verify process is running
        assert pm.is_probe_running() is True
        state = pm.get_pause_state()
        assert state is not None
        assert state.probe_pid == pid

        # Calling ensure_probe_daemon_running again should detect existing running daemon
        same_pid = pm.ensure_probe_daemon_running(script_path=probe_script)
        assert same_pid == pid
    finally:
        # Ensure cleanup of spawned test subprocess
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_5_2_zero_restart_recovery_on_external_replenishment(
    isolated_env: Tuple[Path, Path],
    mock_canopy: DynamicCanopyServer,
):
    """Verify removing pause lock and updating keystore immediately resumes proxy without restart."""
    wallets_path, lock_path = isolated_env
    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    # Initially paused
    pm.pause(reason="Waiting for funds", correlation_id="req_recovery_test")
    assert pm.is_paused() is True

    # Fast-fail is active
    kwargs = {
        "model": "deepseek-v4-pro",
        "api_base": mock_canopy.url,
        "messages": [{"role": "user", "content": "blocked"}],
    }
    with pytest.raises(Exception):
        await guard.async_pre_call_deployment_hook(kwargs)

    # External replenishment simulated:
    # 1. Update wallet state to active in keystore
    replenished_data = {
        "wallets": [
            {
                "id": "wallet_primary",
                "key": "csk_live_backup_222222222222222222222222",
                "type": "personal",
                "state": "active",
                "last_exhausted": None,
            }
        ],
        "active_wallet_id": "wallet_primary",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(replenished_data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)

    # 2. Probe unpauses and unlinks pause lockfile
    pm.unpause()
    assert pm.is_paused() is False

    # 3. Next request succeeds immediately without process restart
    resume_kwargs = {
        "model": "deepseek-v4-pro",
        "api_base": mock_canopy.url,
        "messages": [{"role": "user", "content": "resumed request"}],
    }
    await guard.async_pre_call_deployment_hook(resume_kwargs)

    assert resume_kwargs.get("api_key") == "csk_live_backup_222222222222222222222222"
    assert resume_kwargs.get("stream") is True

    # Dispatch to mock canopy (which accepts csk_live_backup with 200 OK SSE)
    headers = resume_kwargs["extra_headers"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            mock_canopy.url,
            json={"model": resume_kwargs["model"], "messages": resume_kwargs["messages"], "stream": True},
            headers=headers,
            timeout=10.0,
        )
    assert resp.status_code == 200
    assert "Turn completed with Wallet 2!" in resp.text
