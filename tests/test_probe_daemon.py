"""Comprehensive Test Suite for Background Probe Daemon and Wallet CLI Controller.

Covers:
- Tier 1: unstoppable_wallet_probe.sh (Feature F10, Phase 4 task 4.1)
  * PID lock acquisition, single-instance enforcement, and clean trap release
  * Stale PID detection and recovery (kill -0)
  * HTTP 200 credit replenishment detection and lockfile removal
  * osascript desktop notification dispatch and || true failure resilience
  * HTTP 402 / non-200 pause lock retention
  * Mandatory "stream": true in probe curl payload (Interface Contract 4)
  * Configurable polling interval and early exit when unpaused
- Tier 2: wallet_ctl.py CLI Controller (Feature F11, Phase 4 task 4.2)
  * Subcommand 'status': text and --json formatting
  * Subcommand 'reset-lock' / 'unpause': pause lockfile removal
  * Subcommand 'add-key': atomic addition with strict 0600 permissions
  * Subcommand 'mark-exhausted': wallet mutation and active rotation
  * Subcommand 'set-active': explicit active wallet pointer switching
  * Subcommand 'probe': one-shot probe execution and lock updating
- Tier 3: PauseManager & Daemon Lifecycle Integration
  * ensure_probe_daemon_running() detached invocation
  * Fallback to environment keys when keystore is absent
- Tier 4: Critical Invariant Audit
  * Zero forbidden term references across shell script, CLI controller, and test suite.
"""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from service.agentic.endpoint_abstraction.pause_manager import (
    LOCK_PERMISSIONS,
    PauseManager,
)
from service.agentic.endpoint_abstraction.wallet_manager import (
    Wallet,
    WalletManager,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_SCRIPT_PATH = REPO_ROOT / "service" / "bin" / "unstoppable_wallet_probe.sh"
WALLET_CTL_PATH = REPO_ROOT / "service" / "bin" / "wallet_ctl.py"


# ============================================================================ #
# Mock Upstream Canopy Server (127.0.0.1:0)                                    #
# ============================================================================ #


class MockCanopyHandler(BaseHTTPRequestHandler):
    """HTTP request handler simulating Upstream Canopy Proxy responses."""

    @property
    def mock_server(self) -> Optional["MockCanopyServer"]:
        return getattr(self.server, "mock_server", None)

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
            "method": "POST",
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
            "raw": raw,
            "timestamp": time.time(),
        }
        mock_server = self.mock_server
        if mock_server is not None:
            mock_server.record_request(req_record)
            status = mock_server.status_code
            error_payload = mock_server.error_payload
        else:
            status = 200
            error_payload = None

        if status == 200:
            sse_content = (
                b"event: message_start\n"
                b'data: {"type": "message_start", "message": {"id": "msg_probe_ok", "role": "assistant"}}\n\n'
                b"event: content_block_delta\n"
                b'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "P"}}\n\n'
                b"event: message_stop\n"
                b'data: {"type": "message_stop"}\n\n'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("x-canopy-correlation-id", "req_probe_200_ok")
            self.send_header("Content-Length", str(len(sse_content)))
            self.end_headers()
            self.wfile.write(sse_content)
        elif status == 402:
            payload = error_payload or {
                "error": {
                    "type": "payment_required",
                    "message": "personal_wallet_empty",
                    "canopy_proxy": "CANOPY_AI_CREDITS_PROXY",
                    "code": "no_billing_source",
                }
            }
            resp_bytes = json.dumps(payload).encode("utf-8")
            self.send_response(402)
            self.send_header("Content-Type", "application/json")
            self.send_header("x-canopy-correlation-id", "req_canopy_402_depleted")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)
        else:
            resp_bytes = json.dumps({"error": f"HTTP {status}"}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)

    def log_message(self, *args: Any) -> None:
        """Suppress standard HTTP server stderr logging."""
        pass


class MockCanopyServer:
    """Ephemeral mock server bound to dynamic port on 127.0.0.1."""

    def __init__(
        self, status_code: int = 200, error_payload: Optional[Dict[str, Any]] = None
    ) -> None:
        self.status_code = status_code
        self.error_payload = error_payload
        self.recorded_requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockCanopyHandler)
        self.server.mock_server = self  # type: ignore[attr-defined]
        self.host, self.port = self.server.server_address
        self.url = f"http://{self.host}:{self.port}/api/v1/llm-proxy/anthropic/v1/messages"
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "MockCanopyServer":
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

    def __enter__(self) -> "MockCanopyServer":
        return self.start()

    def __exit__(self, *args: Any) -> None:
        self.stop()

    def record_request(self, req: Dict[str, Any]) -> None:
        with self._lock:
            self.recorded_requests.append(req)

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self.recorded_requests)

    @property
    def last_request(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.recorded_requests[-1] if self.recorded_requests else None

    def set_status(
        self, status_code: int, error_payload: Optional[Dict[str, Any]] = None
    ) -> None:
        with self._lock:
            self.status_code = status_code
            self.error_payload = error_payload


# ============================================================================ #
# Test Fixtures: Isolated Filesystem & Mock Notification Environment           #
# ============================================================================ #


@pytest.fixture
def mock_canopy() -> MockCanopyServer:
    """Fixture yielding running MockCanopyServer with automatic cleanup."""
    server = MockCanopyServer(status_code=200)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def probe_env(tmp_path: Path) -> Dict[str, Any]:
    """Provide isolated directory, keystore, pause lock, PID file, and mock osascript."""
    litellm_dir = tmp_path / ".litellm"
    litellm_dir.mkdir(parents=True, exist_ok=True)

    keystore_path = litellm_dir / "unstoppable_wallets.json"
    pause_lock_path = litellm_dir / "unstoppable_pause.lock"
    pid_path = litellm_dir / "unstoppable_probe.pid"

    # Mock osascript executable to capture desktop notifications
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    osascript_log = tmp_path / "osascript.log"
    mock_osascript = bin_dir / "osascript"
    mock_osascript.write_text(
        f'#!/bin/sh\necho "$@" >> "{osascript_log}"\nexit 0\n',
        encoding="utf-8",
    )
    mock_osascript.chmod(0o755)

    # Pre-seed keystore with two test wallets
    initial_keystore = {
        "wallets": [
            {
                "id": "wallet_primary",
                "key": "csk_live_test_secret_key_12345",
                "type": "personal",
                "state": "active",
                "last_exhausted": None,
            },
            {
                "id": "wallet_backup",
                "key": "csk_backup_test_secret_key_67890",
                "type": "org",
                "state": "active",
                "last_exhausted": None,
            },
        ],
        "active_wallet_id": "wallet_primary",
        "updated_at": 1793836800.0,
    }
    keystore_path.write_text(json.dumps(initial_keystore, indent=2), encoding="utf-8")
    os.chmod(keystore_path, 0o600)

    return {
        "root": tmp_path,
        "litellm_dir": litellm_dir,
        "keystore_path": keystore_path,
        "pause_lock_path": pause_lock_path,
        "pid_path": pid_path,
        "bin_dir": bin_dir,
        "osascript_log": osascript_log,
        "mock_osascript": mock_osascript,
    }


def make_subprocess_env(
    probe_env: Dict[str, Any], upstream_url: str, extra: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    """Construct environment dictionary with isolated paths and mock osascript in PATH."""
    env = os.environ.copy()
    env["PATH"] = f"{probe_env['bin_dir']}:{env.get('PATH', '')}"
    env["HOME"] = str(probe_env["root"])
    env["LITELLM_HOME"] = str(probe_env["litellm_dir"])
    env["UNSTOPPABLE_KEYSTORE_PATH"] = str(probe_env["keystore_path"])
    env["UNSTOPPABLE_PAUSE_LOCK_PATH"] = str(probe_env["pause_lock_path"])
    env["UNSTOPPABLE_PROBE_PID_PATH"] = str(probe_env["pid_path"])
    env["UPSTREAM_URL"] = upstream_url
    env["CANOPY_PROXY_URL"] = upstream_url
    if extra:
        env.update(extra)
    return env


# ============================================================================ #
# Tier 1: Probe Daemon Shell Script Tests (unstoppable_wallet_probe.sh)        #
# ============================================================================ #


@pytest.mark.skipif(
    not PROBE_SCRIPT_PATH.is_file(),
    reason="service/bin/unstoppable_wallet_probe.sh not yet installed",
)
def test_pid_lock_acquisition_and_release(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """Daemon creates PID lock file on launch and unlinks it cleanly upon exit."""
    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="Testing PID lifecycle")

    env = make_subprocess_env(probe_env, mock_canopy.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, f"Probe script failed: {res.stderr}"
    assert not probe_env["pid_path"].exists(), "PID file was not cleaned up on clean exit"


@pytest.mark.skipif(
    not PROBE_SCRIPT_PATH.is_file(),
    reason="service/bin/unstoppable_wallet_probe.sh not yet installed",
)
def test_duplicate_instance_prevention(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """Running instance is detected via kill -0; duplicate invocation exits 0 safely."""
    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="Testing duplicate prevention")

    # Record current test process PID as active probe PID
    probe_env["pid_path"].write_text(f"{os.getpid()}\n", encoding="utf-8")

    env = make_subprocess_env(probe_env, mock_canopy.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert "already running" in res.stderr.lower() or "already running" in res.stdout.lower()
    assert mock_canopy.request_count == 0, "Duplicate instance should not execute probe curl"
    assert probe_env["pid_path"].read_text(encoding="utf-8").strip() == str(os.getpid())


@pytest.mark.skipif(
    not PROBE_SCRIPT_PATH.is_file(),
    reason="service/bin/unstoppable_wallet_probe.sh not yet installed",
)
def test_stale_pid_cleanup(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """Stale dead PID is detected and safely overwritten by running instance."""
    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="Testing stale PID")

    dead_pid = 999999  # Guaranteed non-existent PID
    probe_env["pid_path"].write_text(f"{dead_pid}\n", encoding="utf-8")

    env = make_subprocess_env(probe_env, mock_canopy.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert "stale" in res.stderr.lower() or "stale" in res.stdout.lower()
    assert mock_canopy.request_count == 1
    assert not probe_env["pid_path"].exists()


@pytest.mark.skipif(
    not PROBE_SCRIPT_PATH.is_file(),
    reason="service/bin/unstoppable_wallet_probe.sh not yet installed",
)
def test_replenishment_detection_unpauses(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """HTTP 200 from upstream unpauses gateway, deleting pause lockfile and updating keystore."""
    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="personal_wallet_empty", correlation_id="req_test_replenish")
    assert pm.is_paused() is True

    # Mark keystore wallets exhausted to verify replenishment reset
    wm = WalletManager(keystore_path=probe_env["keystore_path"])
    wm.mark_exhausted("wallet_primary")

    mock_canopy.set_status(200)

    env = make_subprocess_env(probe_env, mock_canopy.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert not probe_env["pause_lock_path"].exists(), "Pause lockfile was not removed on HTTP 200"
    assert pm.is_paused() is False

    # Keystore wallets should be reactivated
    wm.reload_from_disk()
    active = wm.get_active_wallet()
    assert active is not None
    assert active.state == "active"


@pytest.mark.skipif(
    not PROBE_SCRIPT_PATH.is_file(),
    reason="service/bin/unstoppable_wallet_probe.sh not yet installed",
)
def test_replenishment_notification(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """HTTP 200 triggers osascript notification with title 'LiteLLM MultiHarness'."""
    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="Testing notification")
    mock_canopy.set_status(200)

    env = make_subprocess_env(probe_env, mock_canopy.url)
    subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert probe_env["osascript_log"].exists(), "osascript was not invoked on replenishment"
    log_content = probe_env["osascript_log"].read_text(encoding="utf-8")
    assert "display notification" in log_content
    assert "LiteLLM MultiHarness" in log_content


@pytest.mark.skipif(
    not PROBE_SCRIPT_PATH.is_file(),
    reason="service/bin/unstoppable_wallet_probe.sh not yet installed",
)
def test_replenishment_notification_osascript_failure_resilience(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """Probe daemon completes unpausing cleanly even if osascript exits with non-zero code."""
    # Write mock osascript that crashes with exit code 1
    probe_env["mock_osascript"].write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    probe_env["mock_osascript"].chmod(0o755)

    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="Testing osascript failure")
    mock_canopy.set_status(200)

    env = make_subprocess_env(probe_env, mock_canopy.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, "Daemon should not fail when osascript fails (requires || true)"
    assert not probe_env["pause_lock_path"].exists()


@pytest.mark.skipif(
    not PROBE_SCRIPT_PATH.is_file(),
    reason="service/bin/unstoppable_wallet_probe.sh not yet installed",
)
def test_continued_depletion_maintains_pause(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """HTTP 402 keeps pause lockfile intact and does not emit notification."""
    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="All wallets exhausted (personal_wallet_empty)")
    mock_canopy.set_status(402)

    env = make_subprocess_env(probe_env, mock_canopy.url)
    subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert probe_env["pause_lock_path"].exists(), "Pause lock must be retained on HTTP 402"
    assert pm.is_paused() is True
    assert not probe_env["osascript_log"].exists(), "Notification must not fire on HTTP 402"


@pytest.mark.skipif(
    not PROBE_SCRIPT_PATH.is_file(),
    reason="service/bin/unstoppable_wallet_probe.sh not yet installed",
)
def test_mandatory_stream_in_probe_curl(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """Probe curl strictly specifies 'stream': true and deepseek-v4-pro per Contract 4."""
    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="Testing contract 4")
    mock_canopy.set_status(200)

    env = make_subprocess_env(probe_env, mock_canopy.url)
    subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    req = mock_canopy.last_request
    assert req is not None
    assert req["headers"].get("Authorization") == "Bearer csk_live_test_secret_key_12345"
    assert req["headers"].get("anthropic-version") == "2023-06-01"
    assert req["headers"].get("Content-Type") == "application/json"

    body = req["body"]
    assert body.get("model") == "deepseek-v4-pro"
    assert body.get("max_tokens") == 1
    assert body.get("stream") is True, "Mandatory 'stream': true missing or not boolean True"
    assert body.get("messages") == [{"role": "user", "content": "probe"}]


@pytest.mark.skipif(
    not PROBE_SCRIPT_PATH.is_file(),
    reason="service/bin/unstoppable_wallet_probe.sh not yet installed",
)
def test_probe_interval_override_and_sigterm_cleanup(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """Daemon respects PROBE_INTERVAL_SECONDS override and cleans up PID file on SIGTERM."""
    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="Testing background loop")
    mock_canopy.set_status(402)  # Maintain pause so daemon keeps looping

    env = make_subprocess_env(
        probe_env, mock_canopy.url, extra={"PROBE_INTERVAL_SECONDS": "0.1"}
    )
    proc = subprocess.Popen(
        ["/bin/bash", str(PROBE_SCRIPT_PATH)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Give daemon brief window to acquire PID lock
        time.sleep(0.3)
        assert probe_env["pid_path"].exists(), "Daemon did not acquire PID lock"
        assert int(probe_env["pid_path"].read_text(encoding="utf-8").strip()) == proc.pid

        # Terminate daemon with SIGTERM
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=3.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert not probe_env["pid_path"].exists(), "PID file not unlinked after SIGTERM"


@pytest.mark.skipif(
    not PROBE_SCRIPT_PATH.is_file(),
    reason="service/bin/unstoppable_wallet_probe.sh not yet installed",
)
def test_probe_when_not_paused_exits_early(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """When lockfile is absent and --force is not given, daemon exits 0 without probing."""
    assert not probe_env["pause_lock_path"].exists()

    env = make_subprocess_env(probe_env, mock_canopy.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert mock_canopy.request_count == 0, "Daemon should not probe when pause lock is absent"


# ============================================================================ #
# Tier 2: CLI Controller Tests (wallet_ctl.py)                                 #
# ============================================================================ #


@pytest.mark.skipif(
    not WALLET_CTL_PATH.is_file(),
    reason="service/bin/wallet_ctl.py not yet installed",
)
def test_wallet_ctl_status_text_and_json(probe_env: Dict[str, Any]) -> None:
    """wallet_ctl status returns human-readable text by default and valid JSON with --json."""
    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="Status test", correlation_id="req_canopy_status_test")

    env = make_subprocess_env(probe_env, "http://127.0.0.1:9999")

    # Text output test
    res_text = subprocess.run(
        [
            sys.executable,
            str(WALLET_CTL_PATH),
            "--config-dir",
            str(probe_env["litellm_dir"]),
            "status",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res_text.returncode == 0
    assert "wallet_primary" in res_text.stdout
    assert "PAUSED" in res_text.stdout
    assert "req_canopy_status_test" in res_text.stdout

    # JSON output test
    res_json = subprocess.run(
        [
            sys.executable,
            str(WALLET_CTL_PATH),
            "--config-dir",
            str(probe_env["litellm_dir"]),
            "status",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res_json.returncode == 0
    data = json.loads(res_json.stdout)
    assert data["active_wallet_id"] == "wallet_primary"
    assert data["total_wallets"] == 2
    assert data["pause_lock"]["paused"] is True
    assert data["pause_lock"]["correlation_id"] == "req_canopy_status_test"


@pytest.mark.skipif(
    not WALLET_CTL_PATH.is_file(),
    reason="service/bin/wallet_ctl.py not yet installed",
)
def test_wallet_ctl_reset_lock(probe_env: Dict[str, Any]) -> None:
    """wallet_ctl reset-lock / unpause unlinks lockfile and resets backpressure."""
    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="Reset lock test")
    assert pm.is_paused() is True

    env = make_subprocess_env(probe_env, "http://127.0.0.1:9999")
    res = subprocess.run(
        [
            sys.executable,
            str(WALLET_CTL_PATH),
            "--config-dir",
            str(probe_env["litellm_dir"]),
            "reset-lock",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert not probe_env["pause_lock_path"].exists()
    assert pm.is_paused() is False


@pytest.mark.skipif(
    not WALLET_CTL_PATH.is_file(),
    reason="service/bin/wallet_ctl.py not yet installed",
)
def test_wallet_ctl_add_key_permissions_0600(probe_env: Dict[str, Any]) -> None:
    """wallet_ctl add-key writes new wallet preserving strict 0600 permissions."""
    env = make_subprocess_env(probe_env, "http://127.0.0.1:9999")
    res = subprocess.run(
        [
            sys.executable,
            str(WALLET_CTL_PATH),
            "--config-dir",
            str(probe_env["litellm_dir"]),
            "add-key",
            "csk_live_brand_new_key_112233",
            "--id",
            "wallet_added_via_cli",
            "--type",
            "org",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    keystore_mode = stat.S_IMODE(probe_env["keystore_path"].stat().st_mode)
    assert keystore_mode == LOCK_PERMISSIONS, f"Expected 0o600, got {oct(keystore_mode)}"

    wm = WalletManager(keystore_path=probe_env["keystore_path"])
    w = wm.get_wallet("wallet_added_via_cli")
    assert w is not None
    assert w.key == "csk_live_brand_new_key_112233"
    assert w.type == "org"


@pytest.mark.skipif(
    not WALLET_CTL_PATH.is_file(),
    reason="service/bin/wallet_ctl.py not yet installed",
)
def test_wallet_ctl_add_key_validation_error(probe_env: Dict[str, Any]) -> None:
    """wallet_ctl add-key rejects invalid/short keys with non-zero exit code."""
    env = make_subprocess_env(probe_env, "http://127.0.0.1:9999")
    res = subprocess.run(
        [
            sys.executable,
            str(WALLET_CTL_PATH),
            "--config-dir",
            str(probe_env["litellm_dir"]),
            "add-key",
            "short",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode != 0
    assert "at least 8" in res.stderr.lower() or "error" in res.stderr.lower()


@pytest.mark.skipif(
    not WALLET_CTL_PATH.is_file(),
    reason="service/bin/wallet_ctl.py not yet installed",
)
def test_wallet_ctl_mark_exhausted(probe_env: Dict[str, Any]) -> None:
    """wallet_ctl mark-exhausted mutates state and advances active wallet pointer."""
    env = make_subprocess_env(probe_env, "http://127.0.0.1:9999")
    res = subprocess.run(
        [
            sys.executable,
            str(WALLET_CTL_PATH),
            "--config-dir",
            str(probe_env["litellm_dir"]),
            "mark-exhausted",
            "wallet_primary",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["status"] == "ok"
    assert data["exhausted_wallet_id"] == "wallet_primary"
    assert data["active_wallet_id"] == "wallet_backup"

    wm = WalletManager(keystore_path=probe_env["keystore_path"])
    assert wm.get_wallet("wallet_primary").state == "exhausted"
    assert wm.active_wallet_id == "wallet_backup"


@pytest.mark.skipif(
    not WALLET_CTL_PATH.is_file(),
    reason="service/bin/wallet_ctl.py not yet installed",
)
def test_wallet_ctl_set_active(probe_env: Dict[str, Any]) -> None:
    """wallet_ctl set-active switches active wallet pointer explicitly."""
    env = make_subprocess_env(probe_env, "http://127.0.0.1:9999")
    res = subprocess.run(
        [
            sys.executable,
            str(WALLET_CTL_PATH),
            "--config-dir",
            str(probe_env["litellm_dir"]),
            "set-active",
            "wallet_backup",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    wm = WalletManager(keystore_path=probe_env["keystore_path"])
    assert wm.active_wallet_id == "wallet_backup"


@pytest.mark.skipif(
    not WALLET_CTL_PATH.is_file(),
    reason="service/bin/wallet_ctl.py not yet installed",
)
def test_wallet_ctl_probe_replenished_unpauses(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """wallet_ctl probe against HTTP 200 upstream unpauses and resets backpressure."""
    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="Testing CLI probe unpause")
    mock_canopy.set_status(200)

    env = make_subprocess_env(probe_env, mock_canopy.url)
    res = subprocess.run(
        [
            sys.executable,
            str(WALLET_CTL_PATH),
            "--config-dir",
            str(probe_env["litellm_dir"]),
            "probe",
            "--upstream-url",
            mock_canopy.url,
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["status"] == "success"
    assert data["http_status"] == 200
    assert data["unpaused"] is True
    assert not probe_env["pause_lock_path"].exists()


@pytest.mark.skipif(
    not WALLET_CTL_PATH.is_file(),
    reason="service/bin/wallet_ctl.py not yet installed",
)
def test_wallet_ctl_probe_exhausted_maintains_lock(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """wallet_ctl probe against HTTP 402 upstream maintains pause lock."""
    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="Testing CLI probe 402 retention")
    mock_canopy.set_status(402)

    env = make_subprocess_env(probe_env, mock_canopy.url)
    res = subprocess.run(
        [
            sys.executable,
            str(WALLET_CTL_PATH),
            "--config-dir",
            str(probe_env["litellm_dir"]),
            "probe",
            "--upstream-url",
            mock_canopy.url,
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["status"] == "exhausted"
    assert data["http_status"] == 402
    assert data["unpaused"] is False
    assert probe_env["pause_lock_path"].exists()


@pytest.mark.skipif(
    not WALLET_CTL_PATH.is_file(),
    reason="service/bin/wallet_ctl.py not yet installed",
)
def test_wallet_ctl_invalid_subcommand_exit_code_2(probe_env: Dict[str, Any]) -> None:
    """Passing unrecognized subcommand returns standard argparse exit code 2."""
    env = make_subprocess_env(probe_env, "http://127.0.0.1:9999")
    res = subprocess.run(
        [sys.executable, str(WALLET_CTL_PATH), "invalid-subcmd"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 2


# ============================================================================ #
# Tier 3: PauseManager & Daemon Lifecycle Integration                          #
# ============================================================================ #


@pytest.mark.skipif(
    not PROBE_SCRIPT_PATH.is_file(),
    reason="service/bin/unstoppable_wallet_probe.sh not yet installed",
)
def test_pause_manager_ensure_probe_daemon_running_integration(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """PauseManager.ensure_probe_daemon_running spawns background daemon script."""
    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="Testing spawn integration")
    mock_canopy.set_status(402)

    spawned_pid = pm.ensure_probe_daemon_running(script_path=PROBE_SCRIPT_PATH)
    assert spawned_pid is not None
    assert spawned_pid > 0

    try:
        # Verify daemon is recorded and active
        assert pm.is_probe_running() is True
    finally:
        try:
            os.kill(spawned_pid, signal.SIGTERM)
        except OSError:
            pass


@pytest.mark.skipif(
    not PROBE_SCRIPT_PATH.is_file(),
    reason="service/bin/unstoppable_wallet_probe.sh not yet installed",
)
def test_probe_script_executes_fallback_key_from_env(
    probe_env: Dict[str, Any], mock_canopy: MockCanopyServer
) -> None:
    """When keystore is missing, probe script resolves active key from CANOPY_API_KEY."""
    if probe_env["keystore_path"].exists():
        probe_env["keystore_path"].unlink()

    pm = PauseManager(lock_path=probe_env["pause_lock_path"])
    pm.pause(reason="Testing env fallback key")
    mock_canopy.set_status(200)

    env = make_subprocess_env(
        probe_env,
        mock_canopy.url,
        extra={"CANOPY_API_KEY": "csk_env_fallback_key_998877"},
    )
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert mock_canopy.request_count == 1
    req = mock_canopy.last_request
    assert req is not None
    assert req["headers"].get("Authorization") == "Bearer csk_env_fallback_key_998877"


# ============================================================================ #
# Tier 4: Critical Invariant Audit                                             #
# ============================================================================ #


def test_zero_forbidden_references_in_m4_suite() -> None:
    """Enforce zero occurrences of forbidden term across M4 scripts and tests."""
    forbidden = "".join(["black", "hole"])
    pattern = re.compile(forbidden, re.IGNORECASE)

    targets = [Path(__file__).resolve()]
    if PROBE_SCRIPT_PATH.is_file():
        targets.append(PROBE_SCRIPT_PATH)
    if WALLET_CTL_PATH.is_file():
        targets.append(WALLET_CTL_PATH)

    for target in targets:
        content = target.read_text(encoding="utf-8")
        assert not pattern.search(content), f"Forbidden term found in {target}"
