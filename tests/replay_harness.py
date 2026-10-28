"""Ephemeral Replay Harness for litellmproxy-multiharness and Antigravity Bridge.

Provides isolated mock servers on dynamic ports (127.0.0.1:0):
- MockLanguageServer: Simulates Antigravity Language Server RPC endpoints
  (/healthz, GetCascadeModelConfigData, GetModelResponse) with configurable behaviors:
  empty, timeout, rpc_error, tool_call, mixed_tool_call, multi_tool_call, normal, custom.
- MockRemoteFallbackServer: Simulates remote fallback deployment
  (antigravity-remote-flash-medium) for failover routing verification.
- EphemeralBridge: Runs service.bin.antigravity_bridge.Handler in an isolated
  thread on dynamic ports wired to MockLanguageServer without touching system processes.
- Replay Utilities: Turn loading, prompt normalization inspection, and SSE parsing.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple, Union
import httpx

from service.bin import antigravity_bridge as bridge


def get_free_port() -> int:
    """Find a dynamic free port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockLanguageServerHandler(BaseHTTPRequestHandler):
    """HTTP request handler for MockLanguageServer."""

    server: "MockLanguageServer"

    def do_GET(self) -> None:
        if self.path in {"/health", "/healthz", "/v1/health"}:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b""
        body: Dict[str, Any] = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = {"raw": raw.decode("utf-8", "replace")}

        req_record = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
            "prompt": body.get("prompt", ""),
            "model": body.get("model", ""),
            "timestamp": time.time(),
        }
        parent: "MockLanguageServer" = getattr(self.server, "parent", None)
        if parent:
            parent.record_request(req_record)

        mode = parent.mode if parent else "normal"
        delay = parent.delay if parent else 0.0

        if "GetCascadeModelConfigData" in self.path:
            catalog = {
                "clientModelConfigs": [
                    {
                        "label": "Antigravity Flash",
                        "modelId": "antigravity-flash",
                        "isRecommended": True,
                        "modelOrAlias": {"model": "MODEL_FLASH"},
                    },
                    {
                        "label": "Antigravity Pro",
                        "modelId": "antigravity-pro",
                        "modelOrAlias": {"model": "MODEL_PRO"},
                    },
                    {
                        "label": "Antigravity Remote Flash Medium",
                        "modelId": "antigravity-remote-flash-medium",
                        "modelOrAlias": {"model": "MODEL_REMOTE_FLASH_MEDIUM"},
                    },
                ],
                "defaultOverrideModelConfig": {
                    "modelOrAlias": {"model": "MODEL_FLASH"}
                },
            }
            self.respond_json(200, catalog)
            return

        if "GetModelResponse" in self.path:
            if delay > 0:
                time.sleep(delay)

            timeout_delay = parent.timeout_delay if parent else 5.0
            custom_response = parent.custom_response if parent else None
            default_text = parent.default_text if parent else "Language server turn processed successfully."

            if mode == "empty":
                # Return empty dict {} - upstream model generated nothing
                self.respond_json(200, {})
                return
            elif mode == "empty_string":
                # Return dict with empty response string
                self.respond_json(200, {"response": ""})
                return
            elif mode == "whitespace":
                self.respond_json(200, {"response": "   \n\t  \n"})
                return
            elif mode == "timeout":
                # Simulate hang or excessive delay
                time.sleep(timeout_delay)
                self.respond_json(200, {"response": "Delayed response"})
                return
            elif mode == "rpc_error":
                self.respond_json(500, {"error": "Simulated upstream RPC failure"})
                return
            elif mode == "connection_drop":
                # Drop socket abruptly
                self.close_connection = True
                return
            elif mode == "tool_call":
                tc_response = (
                    '<tool_call>\n{"name": "create_pieces_memory", '
                    '"arguments": {"text": "IAESTE-Hamburg e.V."}}\n</tool_call>'
                )
                self.respond_json(200, {"response": tc_response})
                return
            elif mode == "mixed_tool_call":
                resp_text = (
                    "Plan formulated.\n"
                    '<tool_call>\n{"name": "Write", "arguments": '
                    '{"file_path": "/tmp/test.txt", "content": "hello"}}\n</tool_call>\n'
                    "File creation initiated."
                )
                self.respond_json(200, {"response": resp_text})
                return
            elif mode == "multi_tool_call":
                resp_text = (
                    '<tool_call>\n{"name": "Write", "arguments": '
                    '{"file_path": "/tmp/1.txt", "content": "one"}}\n</tool_call>\n'
                    '<tool_call>\n{"name": "Write", "arguments": '
                    '{"file_path": "/tmp/2.txt", "content": "two"}}\n</tool_call>'
                )
                self.respond_json(200, {"response": resp_text})
                return
            elif mode == "markdown_json_tool_call":
                resp_text = (
                    "```json\n"
                    '<tool_call>\n{"name": "Write", "arguments": '
                    '{"file_path": "/tmp/test.txt", "content": "hello"}}\n</tool_call>\n'
                    "```"
                )
                self.respond_json(200, {"response": resp_text})
                return
            elif mode == "partial_demarcation":
                resp_text = '<tool_call>{"name": "incomplete"'
                self.respond_json(200, {"response": resp_text})
                return
            elif mode == "custom":
                if callable(custom_response):
                    custom_data = custom_response(body)
                    self.respond_json(200, custom_data)
                elif isinstance(custom_response, dict):
                    self.respond_json(200, custom_response)
                else:
                    self.respond_json(200, {"response": str(custom_response)})
                return
            else:
                # Normal text completion
                normal_text = default_text or "Language server turn processed successfully."
                self.respond_json(200, {"response": normal_text})
                return

        self.respond_json(404, {"error": f"Unknown RPC endpoint: {self.path}"})

    def respond_json(self, status: int, data: Dict[str, Any]) -> None:
        encoded = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args: Any) -> None:
        # Suppress stderr noise in pytest output
        pass


class MockLanguageServer:
    """Ephemeral mock for Antigravity Language Server running on 127.0.0.1:0."""

    def __init__(self, mode: str = "normal", delay: float = 0.0) -> None:
        self.mode = mode
        self.delay = delay
        self.timeout_delay = 5.0
        self.default_text = "Language server turn processed successfully."
        self.custom_response: Optional[Union[Dict[str, Any], Callable[[Dict[str, Any]], Dict[str, Any]]]] = None
        self.recorded_requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self.csrf_token = "mock-csrf-token-12345"

        # Create server bound to ephemeral port
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockLanguageServerHandler)
        self.server.parent = self  # type: ignore[attr-defined]
        self.host = "127.0.0.1"
        self.port = self.server.server_address[1]
        self.address = f"{self.host}:{self.port}"
        self.base_url = f"http://{self.address}"
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "MockLanguageServer":
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

    def __enter__(self) -> "MockLanguageServer":
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def set_mode(
        self,
        mode: str,
        response: Optional[Union[Dict[str, Any], Callable[[Dict[str, Any]], Dict[str, Any]]]] = None,
        delay: float = 0.0,
    ) -> None:
        with self._lock:
            self.mode = mode
            if response is not None:
                self.custom_response = response
            self.delay = delay

    def record_request(self, req: Dict[str, Any]) -> None:
        with self._lock:
            self.recorded_requests.append(req)

    def reset(self) -> None:
        with self._lock:
            self.recorded_requests.clear()
            self.mode = "normal"
            self.delay = 0.0
            self.custom_response = None

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self.recorded_requests)

    @property
    def last_prompt(self) -> str:
        with self._lock:
            if not self.recorded_requests:
                return ""
            return self.recorded_requests[-1].get("prompt", "")

    @property
    def last_request(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self.recorded_requests:
                return None
            return self.recorded_requests[-1]


class MockRemoteFallbackHandler(BaseHTTPRequestHandler):
    """HTTP request handler for MockRemoteFallbackServer."""

    server: "MockRemoteFallbackServer"

    def do_GET(self) -> None:
        if self.path in {"/health", "/healthz", "/v1/health"}:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok", "service": "mock-remote-fallback"}')
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b""
        body: Dict[str, Any] = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = {"raw": raw.decode("utf-8", "replace")}

        req_record = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
            "model": body.get("model", ""),
            "stream": bool(body.get("stream")),
            "timestamp": time.time(),
        }
        parent: "MockRemoteFallbackServer" = getattr(self.server, "parent", None)
        if parent:
            parent.record_request(req_record)

        if self.path in {"/v1/chat/completions", "/chat/completions"}:
            model_name = body.get("model") or "antigravity-remote-flash-medium"
            is_stream = bool(body.get("stream"))
            cmpl_id = f"chatcmpl-remote-{int(time.time()*1000)}"

            if is_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                chunk1 = {
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": "Fallback response from remote server",
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                chunk2 = {
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                self.wfile.write(f"data: {json.dumps(chunk1)}\n\n".encode("utf-8"))
                self.wfile.write(f"data: {json.dumps(chunk2)}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return

            response_data = {
                "id": cmpl_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Fallback response from remote server",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 8,
                    "total_tokens": 20,
                },
            }
            self.respond_json(200, response_data)
            return

        self.respond_json(404, {"error": f"Unknown path: {self.path}"})

    def respond_json(self, status: int, data: Dict[str, Any]) -> None:
        encoded = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args: Any) -> None:
        pass


class MockRemoteFallbackServer:
    """Ephemeral mock for remote fallback deployment running on 127.0.0.1:0."""

    def __init__(self) -> None:
        self.recorded_requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockRemoteFallbackHandler)
        self.server.parent = self  # type: ignore[attr-defined]
        self.host = "127.0.0.1"
        self.port = self.server.server_address[1]
        self.address = f"{self.host}:{self.port}"
        self.base_url = f"http://{self.address}"
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "MockRemoteFallbackServer":
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

    def __enter__(self) -> "MockRemoteFallbackServer":
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def record_request(self, req: Dict[str, Any]) -> None:
        with self._lock:
            self.recorded_requests.append(req)

    def reset(self) -> None:
        with self._lock:
            self.recorded_requests.clear()

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self.recorded_requests)

    @property
    def last_request(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self.recorded_requests:
                return None
            return self.recorded_requests[-1]


class EphemeralBridge:
    """Runs antigravity_bridge.Handler in an ephemeral ThreadingHTTPServer wired to mock_ls."""

    def __init__(self, mock_ls: MockLanguageServer) -> None:
        self.mock_ls = mock_ls
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        self.host = "127.0.0.1"
        self.port = self.server.server_address[1]
        self.base_url = f"http://{self.host}:{self.port}"
        self.completions_url = f"{self.base_url}/v1/chat/completions"
        self._thread: Optional[threading.Thread] = None
        self._orig_cached_info = bridge._cached_ls_info
        self._orig_cached_expiry = bridge._cached_ls_expiry
        self._orig_cached_model_map = bridge._cached_model_map

    def start(self) -> "EphemeralBridge":
        # Wire bridge caching directly to ephemeral mock_ls
        bridge._cached_ls_info = {
            "pid": 99999,
            "address": self.mock_ls.address,
            "csrf_token": self.mock_ls.csrf_token,
        }
        bridge._cached_ls_expiry = float("inf")
        bridge._cached_model_map = None

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
        # Restore bridge original global state
        bridge._cached_ls_info = self._orig_cached_info
        bridge._cached_ls_expiry = self._orig_cached_expiry
        bridge._cached_model_map = self._orig_cached_model_map

    def __enter__(self) -> "EphemeralBridge":
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def get_client(self, timeout: float = 10.0) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=timeout)

    def get_async_client(self, timeout: float = 10.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout)


# ---------------------------------------------------------------------- #
#  Replay & Parsing Utilities                                            #
# ---------------------------------------------------------------------- #


def load_session_fixture(fixture_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Load the reproducible session 7f8bfd5b turns fixture."""
    if fixture_path is None:
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "session_7f8bfd5b_turns.json"
    p = Path(fixture_path)
    if not p.exists():
        raise FileNotFoundError(f"Session fixture not found at {p}")
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_sse_events(sse_text: str) -> List[Dict[str, Any]]:
    """Parse raw SSE text into structured OpenAI completion chunk dicts."""
    events: List[Dict[str, Any]] = []
    lines = sse_text.splitlines()
    for line in lines:
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload_str = line[len("data:") :].strip()
        if payload_str == "[DONE]":
            events.append({"done": True})
            continue
        try:
            chunk = json.loads(payload_str)
            events.append(chunk)
        except Exception:
            pass
    return events


def build_chat_payload(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
    model: str = "antigravity-flash",
    stream: bool = False,
) -> Dict[str, Any]:
    """Construct standard OpenAI POST /v1/chat/completions payload."""
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    return payload
