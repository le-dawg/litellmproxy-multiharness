"""Milestone 5 Adversarial Replay & Stress Testing Suite.

Scope:
- Deep multi-turn message history with tool calls across failovers.
- Rapid multi-wallet rotation churn (3+ wallets) with concurrency and cooldown evaluation.
- Unusual upstream responses (malformed error bodies, mixed 402/502 status, masked errors).

Verification Command:
    cd /Users/thedawgctor/Desktop/dawgctor-personal-tools/litellmproxy-multiharness
    uv run pytest tests/test_adversarial_m5_replay.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest

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
from service.agentic.endpoint_abstraction.pause_manager import PauseManager
from service.agentic.endpoint_abstraction.wallet_manager import Wallet, WalletManager
from tool_choice_guard import StripOrphanToolChoiceGuard, is_failover_error, is_unstoppable_model


# ============================================================================ #
# Configurable Adversarial Mock Server (127.0.0.1:0)                           #
# ============================================================================ #

class AdversarialCanopyHandler(BaseHTTPRequestHandler):
    """Configurable mock HTTP handler for adversarial Canopy response simulation."""

    @property
    def mock_server(self) -> Optional["AdversarialCanopyServer"]:
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
            if mock.custom_handler is not None:
                status, resp_headers, resp_bytes = mock.custom_handler(req_record)
                self.send_response(status)
                for h_name, h_val in resp_headers.items():
                    self.send_header(h_name, h_val)
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                self.wfile.write(resp_bytes)
                return

        # Default key-based behavior
        if "csk_fail_p1" in auth or "csk_live_alpha" in auth:
            self._send_402("req_adv_alpha_exhausted", "personal_wallet_empty")
        elif "csk_fail_p2" in auth:
            self._send_402("req_adv_p2_exhausted", "personal_wallet_empty")
        elif "csk_fail_o1" in auth:
            self._send_402("req_adv_o1_exhausted", "no_billing_source")
        elif "csk_fail_o2" in auth:
            self._send_402("req_adv_o2_exhausted", "personal_wallet_empty")
        elif "csk_healthy" in auth or "csk_live_backup" in auth:
            self._send_200_sse("req_adv_healthy_success")
        else:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "unrecognized_key"}')

    def _send_402(self, correlation_id: str, reason: str) -> None:
        payload = {
            "error": {
                "canopy_proxy": "CANOPY_AI_CREDITS_PROXY",
                "code": "no_billing_source",
                "correlation_id": correlation_id,
                "reason": reason,
                "message": f"AI Credits depleted: {reason}",
            }
        }
        resp_bytes = json.dumps(payload).encode("utf-8")
        self.send_response(402)
        self.send_header("Content-Type", "application/json")
        self.send_header("x-canopy-correlation-id", correlation_id)
        self.send_header("Content-Length", str(len(resp_bytes)))
        self.end_headers()
        self.wfile.write(resp_bytes)

    def _send_200_sse(self, correlation_id: str) -> None:
        sse_content = (
            b"event: message_start\n"
            b'data: {"type": "message_start", "message": {"id": "msg_adv_ok", "role": "assistant"}}\n\n'
            b"event: content_block_delta\n"
            b'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Adversarial turn success"}}\n\n'
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
        pass


class AdversarialCanopyServer:
    """Ephemeral mock server for adversarial tests."""

    def __init__(self) -> None:
        self.recorded_requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self.custom_handler: Optional[Callable[[Dict[str, Any]], Tuple[int, Dict[str, str], bytes]]] = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AdversarialCanopyHandler)
        self.server.mock_server = self  # type: ignore[attr-defined]
        self.host, self.port = self.server.server_address
        self.url = f"http://{self.host}:{self.port}/api/v1/llm-proxy/anthropic/v1/messages"
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "AdversarialCanopyServer":
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

    def record_request(self, req: Dict[str, Any]) -> None:
        with self._lock:
            self.recorded_requests.append(req)

    def set_custom_handler(
        self,
        handler: Optional[Callable[[Dict[str, Any]], Tuple[int, Dict[str, str], bytes]]],
    ) -> None:
        with self._lock:
            self.custom_handler = handler

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self.recorded_requests)

    @property
    def last_request(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.recorded_requests[-1] if self.recorded_requests else None


# ============================================================================ #
# Fixtures                                                                     #
# ============================================================================ #

@pytest.fixture
def isolated_env(tmp_path: Path) -> Tuple[Path, Path]:
    litellm_dir = tmp_path / ".litellm"
    litellm_dir.mkdir(parents=True, exist_ok=True)
    wallets_path = litellm_dir / "unstoppable_wallets.json"
    lock_path = litellm_dir / "unstoppable_pause.lock"
    return wallets_path, lock_path


@pytest.fixture
def mock_server():
    server = AdversarialCanopyServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def isolate_global_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tool_choice_guard.get_unstoppable_code_key", lambda: None)


@pytest.fixture
def five_wallet_pool(isolated_env: Tuple[Path, Path]) -> Tuple[WalletManager, PauseManager]:
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "p1", "key": "csk_fail_p1_11111111", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "p2", "key": "csk_fail_p2_22222222", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "o1", "key": "csk_fail_o1_33333333", "type": "org", "state": "active", "last_exhausted": None},
            {"id": "o2", "key": "csk_fail_o2_44444444", "type": "org", "state": "active", "last_exhausted": None},
            {"id": "o3", "key": "csk_healthy_55555555", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "p1",
        "updated_at": 1793836800.0,
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)
    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    return wm, pm


async def execute_turn_with_adversarial_replay(
    guard: StripOrphanToolChoiceGuard,
    mock_server: AdversarialCanopyServer,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
    model: str = "deepseek-v4-pro",
    max_retries: int = 6,
) -> httpx.Response:
    kwargs: Dict[str, Any] = {
        "model": model,
        "api_base": mock_server.url,
        "messages": messages,
    }
    if tools is not None:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice

    resp: Optional[httpx.Response] = None

    for _ in range(max_retries):
        await guard.async_pre_call_deployment_hook(kwargs)

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

        if resp.status_code == 200:
            await guard.async_post_call_success_hook(kwargs, {}, resp)
            return resp

        # Emulate LiteLLM exception wrapping
        exc: Exception
        if resp.status_code == 402:
            exc = httpx.HTTPStatusError(
                f"Client error '402 Payment Required' for url '{mock_server.url}'",
                request=resp.request,
                response=resp,
            )
        elif resp.status_code == 502:
            exc = httpx.HTTPStatusError(
                f"Server error '502 Bad Gateway' for url '{mock_server.url}'",
                request=resp.request,
                response=resp,
            )
        else:
            exc = httpx.HTTPStatusError(f"HTTP error {resp.status_code}", request=resp.request, response=resp)

        await guard.async_post_call_failure_hook(kwargs, exc, {})

        if guard.wallet_manager and not guard.wallet_manager.has_active_wallet():
            return resp

    assert resp is not None
    return resp


# ============================================================================ #
# Test Dimension 1: Deep Message History & Tool Calling Across Failovers       #
# ============================================================================ #

@pytest.mark.asyncio
async def test_adversarial_deep_20_turn_tool_history_preservation_across_failover(
    isolated_env: Tuple[Path, Path],
    mock_server: AdversarialCanopyServer,
):
    """Stress-test deep multi-turn conversation with nested tool calls across failover.

    Ensures zero loss, zero truncation, and zero mutation of messages or tool specs
    when failover to backup wallet occurs.
    """
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_fail_p1_aaaa", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_healthy_bbbb", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)
    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    # Complex tool suite definitions
    tools = [
        {
            "type": "function",
            "function": {
                "name": "execute_bash",
                "description": "Execute arbitrary shell commands in sandbox",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}},
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file_multiline",
                "description": "Modify file contents with replacement chunk",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
    ]

    # Synthesize deep 20-turn message history with multi-tool calls and thinking blocks
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": "You are a senior systems engineer operating under strict invariants."},
    ]
    for i in range(1, 10):
        messages.extend([
            {"role": "user", "content": f"Step {i}: Inspect system component #{i}"},
            {
                "role": "assistant",
                "content": f"Thinking: analyzing component {i}",
                "tool_calls": [
                    {
                        "id": f"call_bash_{i}_1",
                        "type": "function",
                        "function": {"name": "execute_bash", "arguments": json.dumps({"command": f"stat /comp/{i}"})},
                    },
                    {
                        "id": f"call_bash_{i}_2",
                        "type": "function",
                        "function": {"name": "execute_bash", "arguments": json.dumps({"command": f"md5 /comp/{i}"})},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": f"call_bash_{i}_1",
                "name": "execute_bash",
                "content": f"Component {i} status: OK, mtime: 179383680{i}",
            },
            {
                "role": "tool",
                "tool_call_id": f"call_bash_{i}_2",
                "name": "execute_bash",
                "content": f"MD5 checksum: d41d8cd98f00b204e9800998ecf8427e",
            },
        ])
    messages.append({"role": "user", "content": "Final step: perform deployment."})

    assert len(messages) >= 20

    resp = await execute_turn_with_adversarial_replay(guard, mock_server, messages, tools=tools)
    assert resp.status_code == 200

    # Verify both attempts received the exact same 20+ messages and tools
    assert mock_server.request_count == 2
    req1 = mock_server.recorded_requests[0]
    req2 = mock_server.recorded_requests[1]

    assert "csk_fail_p1" in req1["auth"]
    assert "csk_healthy" in req2["auth"]

    # Verbatim message history check
    assert len(req1["body"]["messages"]) == len(messages)
    assert len(req2["body"]["messages"]) == len(messages)
    assert req2["body"]["messages"] == messages
    assert req2["body"]["tools"] == tools
    assert req2["body"]["stream"] is True

    # Assert active key rotated cleanly
    assert wm.active_wallet_id == "w2"
    assert pm.is_paused() is False


@pytest.mark.asyncio
async def test_adversarial_cascading_failover_across_multiple_turns_with_growing_history(
    isolated_env: Tuple[Path, Path],
    mock_server: AdversarialCanopyServer,
):
    """Verify that in a long conversation, multiple failovers on different turns
    maintain continuous history and update keystore state step by step.
    """
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_fail_p1_turn1", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_fail_p2_turn3", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w3", "key": "csk_healthy_turn_ok", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)
    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    # Dynamic handler:
    # w1 always returns 402
    # w2 returns 200 for turn 1 and turn 2, but 402 on turn 3
    # w3 returns 200
    turn_counter = {"turn": 1}

    def dynamic_handler(req: Dict[str, Any]) -> Tuple[int, Dict[str, str], bytes]:
        auth = req["auth"]
        if "csk_fail_p1" in auth:
            payload = {"error": {"reason": "personal_wallet_empty", "code": "no_billing_source"}}
            return 402, {"Content-Type": "application/json", "x-canopy-correlation-id": "w1_fail"}, json.dumps(payload).encode()
        if "csk_fail_p2" in auth:
            if turn_counter["turn"] < 3:
                sse = b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"
                return 200, {"Content-Type": "text/event-stream"}, sse
            else:
                payload = {"error": {"reason": "personal_wallet_empty", "code": "no_billing_source"}}
                return 402, {"Content-Type": "application/json", "x-canopy-correlation-id": "w2_fail"}, json.dumps(payload).encode()
        if "csk_healthy" in auth:
            sse = b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"
            return 200, {"Content-Type": "text/event-stream"}, sse
        return 403, {"Content-Type": "application/json"}, b'{"error": "bad"}'

    mock_server.set_custom_handler(dynamic_handler)

    # Turn 1: Starts on w1 (402) -> rotates to w2 (200)
    history = [{"role": "user", "content": "Question 1"}]
    resp1 = await execute_turn_with_adversarial_replay(guard, mock_server, history)
    assert resp1.status_code == 200
    assert wm.active_wallet_id == "w2"

    # Turn 2: Starts on w2 -> succeeds immediately (200)
    turn_counter["turn"] = 2
    history.extend([
        {"role": "assistant", "content": "Answer 1"},
        {"role": "user", "content": "Question 2"},
    ])
    resp2 = await execute_turn_with_adversarial_replay(guard, mock_server, history)
    assert resp2.status_code == 200
    assert wm.active_wallet_id == "w2"

    # Turn 3: w2 now returns 402 -> rotates to w3 (200)
    turn_counter["turn"] = 3
    history.extend([
        {"role": "assistant", "content": "Answer 2"},
        {"role": "user", "content": "Question 3 with full history"},
    ])
    resp3 = await execute_turn_with_adversarial_replay(guard, mock_server, history)
    assert resp3.status_code == 200
    assert wm.active_wallet_id == "w3"

    # Check on-disk persistence
    disk_data = json.loads(wallets_path.read_text(encoding="utf-8"))
    assert disk_data["active_wallet_id"] == "w3"
    w1_record = next(w for w in disk_data["wallets"] if w["id"] == "w1")
    w2_record = next(w for w in disk_data["wallets"] if w["id"] == "w2")
    w3_record = next(w for w in disk_data["wallets"] if w["id"] == "w3")
    assert w1_record["state"] == "exhausted"
    assert w2_record["state"] == "exhausted"
    assert w3_record["state"] == "active"


@pytest.mark.asyncio
async def test_adversarial_tool_choice_without_tools_sanitization_in_failover(
    isolated_env: Tuple[Path, Path],
    mock_server: AdversarialCanopyServer,
):
    """Verify tool_choice is safely sanitized without dropping history during failover replay."""
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_fail_p1_x", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_healthy_y", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)
    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    messages = [
        {"role": "user", "content": "Query with tool_choice but no tools"},
        {"role": "assistant", "content": "Reply"},
        {"role": "user", "content": "Follow-up"},
    ]

    # tool_choice provided, but tools is empty list
    resp = await execute_turn_with_adversarial_replay(
        guard,
        mock_server,
        messages,
        tools=[],
        tool_choice="required",
    )
    assert resp.status_code == 200

    last_req = mock_server.last_request
    assert last_req is not None
    assert "tool_choice" not in last_req["body"]
    assert len(last_req["body"]["messages"]) == 3
    assert wm.active_wallet_id == "w2"


# ============================================================================ #
# Test Dimension 2: Rapid Multi-Wallet Rotation Churn (3+ Wallets)             #
# ============================================================================ #

@pytest.mark.asyncio
async def test_adversarial_rapid_sequential_churn_through_5_wallets(
    five_wallet_pool: Tuple[WalletManager, PauseManager],
    mock_server: AdversarialCanopyServer,
    isolated_env: Tuple[Path, Path],
):
    """Rapid sequential churn across 5 wallets:
    p1 (402) -> p2 (402) -> o1 (402) -> o2 (402) -> o3 (200 OK).

    Verifies strict priority ordering (personal first, then org) and atomic persistence.
    """
    wallets_path, lock_path = isolated_env
    wm, pm = five_wallet_pool
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    assert wm.active_wallet_id == "p1"

    messages = [{"role": "user", "content": "Rapid churn test through 5 wallets"}]
    resp = await execute_turn_with_adversarial_replay(guard, mock_server, messages, max_retries=6)

    assert resp.status_code == 200
    assert "Adversarial turn success" in resp.text
    assert mock_server.request_count == 5

    # Check exact progression through wallets
    auths = [r["auth"] for r in mock_server.recorded_requests]
    assert "csk_fail_p1" in auths[0]
    assert "csk_fail_p2" in auths[1]
    assert "csk_fail_o1" in auths[2]
    assert "csk_fail_o2" in auths[3]
    assert "csk_healthy" in auths[4]

    # Verify final in-memory and on-disk state
    assert wm.active_wallet_id == "o3"
    assert pm.is_paused() is False

    disk_data = json.loads(wallets_path.read_text(encoding="utf-8"))
    assert disk_data["active_wallet_id"] == "o3"
    states = {w["id"]: w["state"] for w in disk_data["wallets"]}
    assert states == {"p1": "exhausted", "p2": "exhausted", "o1": "exhausted", "o2": "exhausted", "o3": "active"}


@pytest.mark.asyncio
async def test_adversarial_wallet_manager_direct_concurrency_idempotency(
    isolated_env: Tuple[Path, Path],
):
    """Directly test that WalletManager.mark_exhausted is thread-safe and idempotent
    when called concurrently with a specific wallet_id.
    """
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_fail_p1_conc", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_healthy_conc_2", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w3", "key": "csk_healthy_conc_3", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)
    wm = WalletManager(keystore_path=wallets_path)

    async def call_mark():
        return wm.mark_exhausted("w1")

    await asyncio.gather(*(call_mark() for _ in range(10)))

    assert wm.active_wallet_id == "w2"
    assert wm.get_wallet("w1").state == "exhausted"
    assert wm.get_wallet("w2").state == "active"
    assert wm.get_wallet("w3").state == "active"


@pytest.mark.asyncio
async def test_adversarial_concurrent_wallet_churn_idempotency(
    isolated_env: Tuple[Path, Path],
    mock_server: AdversarialCanopyServer,
):
    """Stress-test concurrent 402 failure reports from 10 parallel tasks on wallet w1.

    Ensures the idempotency guard prevents double-rotation, skipping healthy wallets,
    or filesystem corruption.
    """
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_fail_p1_conc", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_healthy_conc_2", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w3", "key": "csk_healthy_conc_3", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)
    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    req = httpx.Request("POST", mock_server.url)
    resp_402 = httpx.Response(
        status_code=402,
        headers={"x-canopy-correlation-id": "req_conc_test"},
        json={"error": {"reason": "personal_wallet_empty", "code": "no_billing_source"}},
        request=req,
    )
    exc = httpx.HTTPStatusError("402 Payment Required", request=req, response=resp_402)
    request_data = {
        "model": "deepseek-v4-pro",
        "api_base": mock_server.url,
        "api_key": "csk_fail_p1_conc",
        "extra_headers": {"Authorization": "Bearer csk_fail_p1_conc"},
    }

    # Simulate 10 simultaneous failure reports on wallet w1
    async def report_failure():
        await guard.async_post_call_failure_hook(request_data, exc, {})

    await asyncio.gather(*(report_failure() for _ in range(10)))

    # CRITICAL: Active wallet must have rotated to w2 and STOPPED there
    # It must NOT have rotated through w2 to w3
    assert wm.active_wallet_id == "w2"
    assert pm.is_paused() is False

    disk_data = json.loads(wallets_path.read_text(encoding="utf-8"))
    assert disk_data["active_wallet_id"] == "w2"
    w1_rec = next(w for w in disk_data["wallets"] if w["id"] == "w1")
    w2_rec = next(w for w in disk_data["wallets"] if w["id"] == "w2")
    w3_rec = next(w for w in disk_data["wallets"] if w["id"] == "w3")
    assert w1_rec["state"] == "exhausted"
    assert w2_rec["state"] == "active"
    assert w3_rec["state"] == "active"



@pytest.mark.asyncio
async def test_adversarial_total_pool_churn_engages_pause_lock_and_contract3(
    isolated_env: Tuple[Path, Path],
    mock_server: AdversarialCanopyServer,
):
    """Rapid churn through all wallets in pool until exhaustion engages pause lock and Contract 3 fast-fail."""
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_fail_p1_all", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_fail_p2_all", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)
    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    messages = [{"role": "user", "content": "Exhaust everything"}]
    resp = await execute_turn_with_adversarial_replay(guard, mock_server, messages, max_retries=5)
    assert resp.status_code == 402

    # Pause lock engaged
    assert pm.is_paused() is True
    assert lock_path.is_file()

    # Pre-call hook immediately fast-fails with Contract 3
    with pytest.raises(Exception) as exc_info:
        await guard.async_pre_call_deployment_hook({"model": "deepseek-v4-pro", "api_base": mock_server.url})

    exc = exc_info.value
    assert getattr(exc, "status_code", None) == 402
    detail = getattr(exc, "detail", {})
    err = detail.get("error", {})
    assert err.get("message") == DEFAULT_DEPLETED_MESSAGE
    assert err.get("type") == "payment_required"
    assert err.get("code") == 402
    assert err.get("status") == "paused"
    assert err.get("resume_instruction") == DEFAULT_RESUME_INSTRUCTION


# ============================================================================ #
# Test Dimension 3: Unusual Upstream Responses & Mixed Status Codes           #
# ============================================================================ #

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_body,body_desc",
    [
        (b"", "empty body"),
        (b"<html><body><h1>402 Payment Required</h1><p>Nginx proxy error</p></body></html>", "raw html body"),
        (b'{"error": {"code": "no_billing_source", "reason": "personal_wal', "truncated broken json"),
        (b'[402, "payment_required", {"nested": "list"}]', "json array"),
        (b'"just a raw json string"', "json string"),
        (b'{"error": null, "code": 12345, "reason": true}', "unexpected json types"),
        (b'{"raw_bytes": "\x00\x01\x02\xff"}', "binary escape sequence"),
    ],
)
async def test_adversarial_malformed_402_bodies_do_not_crash_and_trigger_failover(
    isolated_env: Tuple[Path, Path],
    mock_server: AdversarialCanopyServer,
    malformed_body: bytes,
    body_desc: str,
):
    """Stress-test classifier and guard resilience when upstream returns malformed 402 bodies.

    Verifies that neither json decoding exceptions nor unexpected types crash the proxy,
    and failover to healthy wallet w2 occurs reliably.
    """
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_fail_p1_malformed", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_healthy_malformed", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)
    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    def malformed_handler(req: Dict[str, Any]) -> Tuple[int, Dict[str, str], bytes]:
        auth = req["auth"]
        if "csk_fail_p1" in auth:
            headers = {
                "Content-Type": "application/json" if b"{" in malformed_body else "text/html",
                "x-canopy-correlation-id": "req_malformed_test_123",
            }
            return 402, headers, malformed_body
        else:
            sse = b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"
            return 200, {"Content-Type": "text/event-stream"}, sse

    mock_server.set_custom_handler(malformed_handler)

    messages = [{"role": "user", "content": f"Testing malformed 402 body: {body_desc}"}]
    resp = await execute_turn_with_adversarial_replay(guard, mock_server, messages)

    # Must succeed via Wallet 2
    assert resp.status_code == 200
    assert wm.active_wallet_id == "w2"
    assert pm.is_paused() is False


@pytest.mark.asyncio
async def test_adversarial_mixed_502_bad_gateway_and_402_exhaustion_sequence(
    isolated_env: Tuple[Path, Path],
    mock_server: AdversarialCanopyServer,
):
    """Verify that an upstream 502 Bad Gateway does NOT exhaust active wallets,
    while subsequent 402 properly triggers failover.
    """
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_fail_p1_mixed", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_healthy_mixed", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)
    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    step_stage = {"stage": "502_error"}

    def mixed_handler(req: Dict[str, Any]) -> Tuple[int, Dict[str, str], bytes]:
        auth = req["auth"]
        if "csk_fail_p1" in auth:
            if step_stage["stage"] == "502_error":
                return 502, {"Content-Type": "text/plain"}, b"502 Bad Gateway: Upstream gateway connect timeout"
            else:
                payload = {"error": {"reason": "personal_wallet_empty", "code": "no_billing_source"}}
                return 402, {"Content-Type": "application/json"}, json.dumps(payload).encode()
        else:
            sse = b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"
            return 200, {"Content-Type": "text/event-stream"}, sse

    mock_server.set_custom_handler(mixed_handler)

    # Stage 1: Upstream 502 on w1
    messages = [{"role": "user", "content": "Testing 502 gateway error"}]
    resp_502 = await execute_turn_with_adversarial_replay(guard, mock_server, messages, max_retries=1)
    assert resp_502.status_code == 502

    # w1 must NOT be marked exhausted because 502 is a network/gateway failure, not credit exhaustion!
    assert wm.active_wallet_id == "w1"
    w1_wallet = wm.get_active_wallet()
    assert w1_wallet is not None
    assert w1_wallet.state == "active"
    assert pm.is_paused() is False

    # Stage 2: Upstream now returns 402 on w1
    step_stage["stage"] = "402_exhaustion"
    resp_402 = await execute_turn_with_adversarial_replay(guard, mock_server, messages, max_retries=3)
    assert resp_402.status_code == 200
    assert wm.active_wallet_id == "w2"


@pytest.mark.asyncio
async def test_adversarial_masked_402_inside_502_gateway_payload(
    isolated_env: Tuple[Path, Path],
    mock_server: AdversarialCanopyServer,
):
    """When an upstream proxy returns HTTP 502 but the payload body contains Canopy
    personal_wallet_empty markers, Tier 2 payload classification must detect it as credit
    exhaustion and rotate the wallet.
    """
    wallets_path, lock_path = isolated_env
    data = {
        "wallets": [
            {"id": "w1", "key": "csk_fail_p1_masked502", "type": "personal", "state": "active", "last_exhausted": None},
            {"id": "w2", "key": "csk_healthy_masked502", "type": "org", "state": "active", "last_exhausted": None},
        ],
        "active_wallet_id": "w1",
        "updated_at": time.time(),
    }
    wallets_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(wallets_path, 0o600)
    wm = WalletManager(keystore_path=wallets_path)
    pm = PauseManager(lock_path=lock_path)
    guard = StripOrphanToolChoiceGuard(wallet_manager=wm, pause_manager=pm)

    def masked_502_handler(req: Dict[str, Any]) -> Tuple[int, Dict[str, str], bytes]:
        auth = req["auth"]
        if "csk_fail_p1" in auth:
            payload = {
                "error": {
                    "canopy_proxy": "CANOPY_AI_CREDITS_PROXY",
                    "code": "no_billing_source",
                    "reason": "personal_wallet_empty",
                    "message": "AI Credits depleted disguised in 502",
                }
            }
            return 502, {"Content-Type": "application/json", "x-canopy-correlation-id": "req_502_canopy"}, json.dumps(payload).encode()
        else:
            sse = b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"
            return 200, {"Content-Type": "text/event-stream"}, sse

    mock_server.set_custom_handler(masked_502_handler)

    messages = [{"role": "user", "content": "Testing 502 containing Canopy payload markers"}]
    resp = await execute_turn_with_adversarial_replay(guard, mock_server, messages)

    assert resp.status_code == 200
    assert wm.active_wallet_id == "w2"


def test_adversarial_giant_payload_and_deep_exception_chain():
    """Verify CanopyClassifier handles 100KB+ payloads and deeply chained exceptions without recursion error."""
    # 1. Giant payload test
    giant_text = "x" * 100000 + ' {"error": {"canopy_proxy": "CANOPY_AI_CREDITS_PROXY", "reason": "personal_wallet_empty"}}'
    assert CanopyClassifier.is_canopy_exhaustion(giant_text) is True
    assert CanopyClassifier.extract_exhaustion_reason(giant_text) == "personal_wallet_empty"

    # 2. Deep chained exceptions (depth 8)
    class SimulatedUpstreamError(Exception):
        pass

    curr: Exception = SimulatedUpstreamError("AnthropicException: 402 payment required personal_wallet_empty")
    for d in range(8):
        new_err = RuntimeError(f"Chained wrapper depth {d}")
        new_err.__cause__ = curr
        curr = new_err

    assert CanopyClassifier.is_canopy_exhaustion(curr) is True


