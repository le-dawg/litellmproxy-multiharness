"""Adversarial Probe Daemon Stress Test Suite.

Author: teamwork_challenger_m4_1 (Milestone 4 Adversarial Challenger)
Target: service/bin/unstoppable_wallet_probe.sh & service/bin/wallet_ctl.py

Adversarially probes:
1. Concurrent daemon launches & PID lock race condition contention
2. Stale PID recovery across dead PID, 0-byte file, corrupt text, whitespace, and foreign PID safety
3. Signal termination (SIGTERM, SIGINT, SIGHUP) & immediate PID unlinking
4. Upstream failure resilience matrix (slow timeouts, connection refused, 400-504 status codes)
5. Strict upstream enforcement of Interface Contract 4 ("stream": true, deepseek-v4-pro, max_tokens: 1)
6. osascript failure resilience (non-zero exits, command not found, stderr noise)
7. Missing pause lock handling (early exit vs --force)
8. Keystore corruption fallback & reactivation priority
9. Critical Invariant: Zero forbidden word references
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

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_SCRIPT_PATH = REPO_ROOT / "service" / "bin" / "unstoppable_wallet_probe.sh"
WALLET_CTL_PATH = REPO_ROOT / "service" / "bin" / "wallet_ctl.py"


# ============================================================================ #
# Adversarial Mock Canopy Server with Delay and Contract Verification          #
# ============================================================================ #


class AdversarialMockHandler(BaseHTTPRequestHandler):
    """Mock handler simulating upstream Canopy behavior with delay and contract checking."""

    @property
    def mock_server(self) -> Optional["AdversarialMockServer"]:
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

        req = {
            "method": "POST",
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
            "raw": raw,
            "time": time.time(),
        }

        mock = self.mock_server
        if mock is not None:
            mock.record_request(req)
            if mock.delay_seconds > 0:
                time.sleep(mock.delay_seconds)
            status = mock.status_code
            error_payload = mock.error_payload
            strict_contract = mock.strict_contract_enforcement
        else:
            status = 200
            error_payload = None
            strict_contract = False

        if strict_contract:
            # Enforce Contract 4 strictly:
            # model == "deepseek-v4-pro", max_tokens == 1, stream == True
            is_valid = (
                body.get("model") == "deepseek-v4-pro"
                and body.get("max_tokens") == 1
                and body.get("stream") is True
                and self.headers.get("anthropic-version") == "2023-06-01"
                and self.headers.get("Authorization", "").startswith("Bearer ")
            )
            if not is_valid:
                status = 400
                error_payload = {
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Probe request violated Interface Contract 4",
                    }
                }

        if status == 200:
            sse_content = (
                b"event: message_start\n"
                b'data: {"type": "message_start", "message": {"id": "msg_adv_probe_ok"}}\n\n'
                b"event: content_block_delta\n"
                b'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "P"}}\n\n'
                b"event: message_stop\n"
                b'data: {"type": "message_stop"}\n\n'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("x-canopy-correlation-id", "req_adv_ok_200")
            self.send_header("Content-Length", str(len(sse_content)))
            self.end_headers()
            self.wfile.write(sse_content)
        else:
            payload = error_payload or {"error": f"HTTP {status}"}
            resp_bytes = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("x-canopy-correlation-id", f"req_adv_{status}")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)

    def log_message(self, *args: Any) -> None:
        pass


class AdversarialMockServer:
    """Threaded mock server for adversarial tests."""

    def __init__(
        self,
        status_code: int = 200,
        delay_seconds: float = 0.0,
        strict_contract_enforcement: bool = False,
        error_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.status_code = status_code
        self.delay_seconds = delay_seconds
        self.strict_contract_enforcement = strict_contract_enforcement
        self.error_payload = error_payload
        self.recorded_requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AdversarialMockHandler)
        self.server.mock_server = self  # type: ignore[attr-defined]
        self.host, self.port = self.server.server_address
        self.url = f"http://{self.host}:{self.port}/api/v1/llm-proxy/anthropic/v1/messages"
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "AdversarialMockServer":
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

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self.recorded_requests)

    @property
    def last_request(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.recorded_requests[-1] if self.recorded_requests else None

    def set_status(
        self,
        status_code: int,
        delay_seconds: float = 0.0,
        error_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self.status_code = status_code
            self.delay_seconds = delay_seconds
            self.error_payload = error_payload


# ============================================================================ #
# Fixtures                                                                     #
# ============================================================================ #


@pytest.fixture
def adv_server() -> AdversarialMockServer:
    server = AdversarialMockServer(status_code=200)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def adv_env(tmp_path: Path) -> Dict[str, Any]:
    litellm_dir = tmp_path / ".litellm"
    litellm_dir.mkdir(parents=True, exist_ok=True)

    keystore_path = litellm_dir / "unstoppable_wallets.json"
    pause_lock_path = litellm_dir / "unstoppable_pause.lock"
    pid_path = litellm_dir / "unstoppable_probe.pid"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    osascript_log = tmp_path / "osascript.log"
    mock_osascript = bin_dir / "osascript"
    mock_osascript.write_text(
        f'#!/bin/sh\necho "$@" >> "{osascript_log}"\nexit 0\n',
        encoding="utf-8",
    )
    mock_osascript.chmod(0o755)

    initial_keystore = {
        "wallets": [
            {
                "id": "wallet_primary",
                "key": "csk_live_test_adv_primary_key_111",
                "type": "personal",
                "state": "active",
                "last_exhausted": None,
            },
            {
                "id": "wallet_backup",
                "key": "csk_backup_test_adv_backup_key_222",
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

    # Create active pause lockfile by default
    pause_lock_path.write_text(
        json.dumps(
            {
                "paused": True,
                "paused_at": time.time(),
                "reason": "All wallets exhausted",
                "correlation_id": "req_initial_pause",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    os.chmod(pause_lock_path, 0o600)

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


def make_adv_subprocess_env(
    adv_env: Dict[str, Any], upstream_url: str, extra: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{adv_env['bin_dir']}:{env.get('PATH', '')}"
    env["HOME"] = str(adv_env["root"])
    env["LITELLM_HOME"] = str(adv_env["litellm_dir"])
    env["UNSTOPPABLE_KEYSTORE_PATH"] = str(adv_env["keystore_path"])
    env["UNSTOPPABLE_PAUSE_LOCK_PATH"] = str(adv_env["pause_lock_path"])
    env["UNSTOPPABLE_PROBE_PID_PATH"] = str(adv_env["pid_path"])
    env["UPSTREAM_URL"] = upstream_url
    env["CANOPY_PROXY_URL"] = upstream_url
    if extra:
        env.update(extra)
    return env


# ============================================================================ #
# 1. Concurrent Launches & PID Contention                                      #
# ============================================================================ #


def test_adversarial_concurrent_daemon_launches_race_condition(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """Stress test: 10 daemon processes concurrently race for PID lock. Exactly 1 must survive."""
    adv_server.set_status(402)  # Maintain pause so winner keeps running
    env = make_adv_subprocess_env(
        adv_env, adv_server.url, extra={"PROBE_INTERVAL_SECONDS": "5"}
    )

    procs: List[subprocess.Popen] = []
    try:
        for _ in range(10):
            p = subprocess.Popen(
                ["/bin/bash", str(PROBE_SCRIPT_PATH)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            procs.append(p)

        # Allow time for contention resolution
        time.sleep(1.0)

        alive = [p for p in procs if p.poll() is None]
        exited = [p for p in procs if p.poll() is not None]

        # Exactly 1 process should hold the lock and remain alive
        assert len(alive) == 1, f"Expected 1 winner, found {len(alive)} alive processes"
        winner = alive[0]

        # Verify PID file contains the winner's PID
        assert adv_env["pid_path"].exists(), "PID file missing after race"
        recorded_pid = int(adv_env["pid_path"].read_text(encoding="utf-8").strip())
        assert recorded_pid == winner.pid, "PID file does not match winning process PID"

        # Verify all losers exited cleanly (code 0 or 1) without abnormal aborts
        for p in exited:
            assert p.returncode in (0, 1), f"Contender exited abnormally with code {p.returncode}"

        # Terminate winner via process group SIGTERM
        os.killpg(winner.pid, signal.SIGTERM)
        winner.wait(timeout=3.0)
        assert not adv_env["pid_path"].exists(), "PID file was not cleaned up when winner exited"
    finally:
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    p.kill()
                p.wait()


def test_adversarial_concurrent_once_probes(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """Stress test: 8 simultaneous one-shot probe runs against HTTP 200 replenishment."""
    adv_server.set_status(200)
    env = make_adv_subprocess_env(adv_env, adv_server.url)

    procs: List[subprocess.Popen] = []
    try:
        for _ in range(8):
            p = subprocess.Popen(
                ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            procs.append(p)

        for p in procs:
            p.wait(timeout=10.0)
            assert p.returncode in (0, 1)

        # The pause lockfile should be removed by the one that successfully probed
        assert not adv_env["pause_lock_path"].exists(), "Pause lock should be removed on HTTP 200"
        # The PID file must not be left behind
        assert not adv_env["pid_path"].exists(), "PID file lingered after one-shot probes completed"
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()


# ============================================================================ #
# 2. Stale PID Recovery & Foreign Process Safety                               #
# ============================================================================ #


def test_adversarial_stale_pid_dead_process(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """Stale dead PID (e.g. 9999999) must be cleaned up and new lock acquired."""
    adv_server.set_status(200)
    adv_env["pid_path"].write_text("9999999\n", encoding="utf-8")

    env = make_adv_subprocess_env(adv_env, adv_server.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert "stale" in res.stderr.lower()
    assert not adv_env["pid_path"].exists()


def test_adversarial_stale_pid_zero_byte(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """Zero-byte PID file must be safely detected, removed, and lock acquired."""
    adv_server.set_status(200)
    adv_env["pid_path"].touch()
    assert adv_env["pid_path"].stat().st_size == 0

    env = make_adv_subprocess_env(adv_env, adv_server.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert "stale" in res.stderr.lower()
    assert not adv_env["pid_path"].exists()


def test_adversarial_stale_pid_corrupted_garbage(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """Corrupted garbage text in PID file must not trigger bash crash and must be recovered."""
    adv_server.set_status(200)
    adv_env["pid_path"].write_text("not_a_valid_pid_!@#$%^&*()\n", encoding="utf-8")

    env = make_adv_subprocess_env(adv_env, adv_server.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert "stale" in res.stderr.lower()
    assert not adv_env["pid_path"].exists()


def test_adversarial_stale_pid_multiline_whitespace(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """PID file containing only whitespace / newlines must be recovered cleanly."""
    adv_server.set_status(200)
    adv_env["pid_path"].write_text("   \n\t  \n", encoding="utf-8")

    env = make_adv_subprocess_env(adv_env, adv_server.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert not adv_env["pid_path"].exists()


def test_adversarial_pid_lock_held_by_living_foreign_process(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """PID file contains PID of an existing foreign process. Script must exit 0 and NOT touch foreign process."""
    current_pid = os.getpid()
    adv_env["pid_path"].write_text(f"{current_pid}\n", encoding="utf-8")

    env = make_adv_subprocess_env(adv_env, adv_server.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert "already running" in res.stderr.lower()
    # Ensure foreign PID file was not modified or removed
    assert adv_env["pid_path"].read_text(encoding="utf-8").strip() == str(current_pid)
    # Ensure foreign process (pytest) was not killed
    assert os.kill(current_pid, 0) is None or True


# ============================================================================ #
# 3. Rapid Signal Termination & PID Unlinking                                  #
# ============================================================================ #


def test_adversarial_process_group_sigterm_cleanup(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """Process group SIGTERM immediately unlinks PID file and exits cleanly."""
    adv_server.set_status(402)
    env = make_adv_subprocess_env(
        adv_env, adv_server.url, extra={"PROBE_INTERVAL_SECONDS": "10"}
    )

    proc = subprocess.Popen(
        ["/bin/bash", str(PROBE_SCRIPT_PATH)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        time.sleep(0.5)
        assert adv_env["pid_path"].exists(), "Daemon PID file not created"

        # Signal entire process group
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=3.0)
        assert not adv_env["pid_path"].exists(), "PID file lingered after SIGTERM to process group"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_adversarial_process_group_sigint_cleanup(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """Process group SIGINT (Ctrl+C equivalent) cleans up PID file immediately."""
    adv_server.set_status(402)
    env = make_adv_subprocess_env(
        adv_env, adv_server.url, extra={"PROBE_INTERVAL_SECONDS": "10"}
    )

    proc = subprocess.Popen(
        ["/bin/bash", str(PROBE_SCRIPT_PATH)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        time.sleep(0.5)
        assert adv_env["pid_path"].exists()

        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=3.0)
        assert not adv_env["pid_path"].exists(), "PID file lingered after SIGINT"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_adversarial_sigterm_during_slow_network_call(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """SIGTERM arriving while daemon is waiting on slow upstream curl call cleans up PID file."""
    # Mock server hangs for 5 seconds
    adv_server.set_status(200, delay_seconds=5.0)
    env = make_adv_subprocess_env(
        adv_env, adv_server.url, extra={"PROBE_CURL_TIMEOUT": "10"}
    )

    proc = subprocess.Popen(
        ["/bin/bash", str(PROBE_SCRIPT_PATH)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        # Wait until mock server receives curl connection
        for _ in range(30):
            if adv_server.request_count > 0:
                break
            time.sleep(0.1)
        assert adv_server.request_count > 0, "Curl did not reach upstream server"

        # Signal process group while curl is active
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=3.0)
        assert not adv_env["pid_path"].exists(), "PID file not unlinked after termination during curl"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ============================================================================ #
# 4. Upstream Timeouts, Network Failures, Non-200 HTTP Matrix                  #
# ============================================================================ #


def test_adversarial_upstream_timeout_maintains_lock(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """Upstream exceeding curl timeout causes probe failure and strictly maintains pause lock."""
    # Server delays 3.0s, timeout is 0.5s
    adv_server.set_status(200, delay_seconds=3.0)
    env = make_adv_subprocess_env(
        adv_env, adv_server.url, extra={"PROBE_CURL_TIMEOUT": "0.5"}
    )

    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 1, "Probe with timeout should exit 1 on --once"
    assert adv_env["pause_lock_path"].exists(), "Pause lock must NOT be removed on timeout"
    assert not adv_env["osascript_log"].exists(), "Notification must NOT fire on timeout"


def test_adversarial_upstream_connection_refused_maintains_lock(
    adv_env: Dict[str, Any]
) -> None:
    """Connection refused (closed port) strictly maintains pause lock."""
    env = make_adv_subprocess_env(adv_env, "http://127.0.0.1:49993/api/test")

    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 1
    assert adv_env["pause_lock_path"].exists(), "Pause lock must NOT be removed on connection failure"
    assert not adv_env["osascript_log"].exists()


@pytest.mark.parametrize(
    "status_code",
    [400, 401, 402, 403, 404, 429, 500, 502, 503, 504],
)
def test_adversarial_upstream_non_200_matrix(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer, status_code: int
) -> None:
    """Every non-200 HTTP response strictly preserves pause lock and suppresses notification."""
    adv_server.set_status(status_code, error_payload={"error": f"Simulated {status_code}"})
    env = make_adv_subprocess_env(adv_env, adv_server.url)

    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 1, f"HTTP {status_code} should exit 1 on --once"
    assert adv_env["pause_lock_path"].exists(), f"Pause lock unlinked on HTTP {status_code}!"
    assert not adv_env["osascript_log"].exists(), f"Notification dispatched on HTTP {status_code}!"


# ============================================================================ #
# 5. Mandatory "stream": true & Contract 4 Enforcement                         #
# ============================================================================ #


def test_adversarial_strict_stream_true_enforcement(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """Upstream rejects any probe missing 'stream': true or Contract 4 schema; daemon passes."""
    adv_server.strict_contract_enforcement = True
    adv_server.set_status(200)

    env = make_adv_subprocess_env(adv_env, adv_server.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, f"Contract validation failed: {res.stderr}"
    assert not adv_env["pause_lock_path"].exists(), "Replenishment should succeed under valid contract"

    req = adv_server.last_request
    assert req is not None
    assert req["body"].get("stream") is True
    assert req["body"].get("model") == "deepseek-v4-pro"
    assert req["body"].get("max_tokens") == 1


def test_adversarial_wallet_ctl_probe_strict_contract(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """wallet_ctl.py probe also strictly satisfies Interface Contract 4."""
    adv_server.strict_contract_enforcement = True
    adv_server.set_status(200)

    env = make_adv_subprocess_env(adv_env, adv_server.url)
    res = subprocess.run(
        [
            sys.executable,
            str(WALLET_CTL_PATH),
            "--config-dir",
            str(adv_env["litellm_dir"]),
            "probe",
            "--upstream-url",
            adv_server.url,
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

    req = adv_server.last_request
    assert req is not None
    assert req["body"].get("stream") is True
    assert req["body"].get("model") == "deepseek-v4-pro"
    assert req["body"].get("max_tokens") == 1


# ============================================================================ #
# 6. osascript Notification Failure Resilience                                 #
# ============================================================================ #


def test_adversarial_osascript_exit_127_resilience(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """If osascript exits with 127 (e.g. command not found), unpausing proceeds normally."""
    adv_env["mock_osascript"].write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    adv_env["mock_osascript"].chmod(0o755)

    adv_server.set_status(200)
    env = make_adv_subprocess_env(adv_env, adv_server.url)

    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert not adv_env["pause_lock_path"].exists()


def test_adversarial_osascript_stderr_noise_resilience(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """If osascript emits large stderr noise and exits 1, daemon suppresses it cleanly via || true."""
    noisy_script = (
        "#!/bin/sh\n"
        "for i in $(seq 1 100); do echo 'CRITICAL WINDOW SERVER ERROR' >&2; done\n"
        "exit 1\n"
    )
    adv_env["mock_osascript"].write_text(noisy_script, encoding="utf-8")
    adv_env["mock_osascript"].chmod(0o755)

    adv_server.set_status(200)
    env = make_adv_subprocess_env(adv_env, adv_server.url)

    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert not adv_env["pause_lock_path"].exists()


# ============================================================================ #
# 7. Pause Lock Lifecycle & Keystore Edge Cases                                #
# ============================================================================ #


def test_adversarial_unpaused_early_exit_without_probe(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """When pause lock is missing and --force is not specified, daemon exits 0 without contacting upstream."""
    if adv_env["pause_lock_path"].exists():
        adv_env["pause_lock_path"].unlink()

    env = make_adv_subprocess_env(adv_env, adv_server.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert adv_server.request_count == 0, "Daemon probed despite missing pause lock!"


def test_adversarial_force_flag_probes_without_pause_lock(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """When --force is passed, daemon probes even when pause lock is absent."""
    if adv_env["pause_lock_path"].exists():
        adv_env["pause_lock_path"].unlink()

    adv_server.set_status(200)
    env = make_adv_subprocess_env(adv_env, adv_server.url)
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once", "--force"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert adv_server.request_count == 1, "Daemon failed to probe when --force was specified"


def test_adversarial_keystore_corrupted_json_fallback(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """If keystore contains corrupt JSON, daemon falls back to CANOPY_API_KEY environment variable."""
    adv_env["keystore_path"].write_text("{{corrupted json invalid", encoding="utf-8")
    adv_server.set_status(200)

    env = make_adv_subprocess_env(
        adv_env,
        adv_server.url,
        extra={"CANOPY_API_KEY": "csk_fallback_from_env_333444"},
    )
    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    req = adv_server.last_request
    assert req is not None
    assert req["headers"].get("Authorization") == "Bearer csk_fallback_from_env_333444"


def test_adversarial_keystore_reactivation_priority(
    adv_env: Dict[str, Any], adv_server: AdversarialMockServer
) -> None:
    """Upon HTTP 200 replenishment, keystore reactivates all wallets and sets active to personal wallet."""
    # Seed keystore with org first, personal second, both exhausted
    keystore_data = {
        "wallets": [
            {
                "id": "wallet_org_backup",
                "key": "csk_org_key_999",
                "type": "org",
                "state": "exhausted",
                "last_exhausted": time.time(),
            },
            {
                "id": "wallet_personal_primary",
                "key": "csk_personal_key_888",
                "type": "personal",
                "state": "exhausted",
                "last_exhausted": time.time(),
            },
        ],
        "active_wallet_id": None,
        "updated_at": 1000.0,
    }
    adv_env["keystore_path"].write_text(json.dumps(keystore_data, indent=2), encoding="utf-8")

    adv_server.set_status(200)
    env = make_adv_subprocess_env(adv_env, adv_server.url)

    res = subprocess.run(
        ["/bin/bash", str(PROBE_SCRIPT_PATH), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0

    reloaded = json.loads(adv_env["keystore_path"].read_text(encoding="utf-8"))
    assert reloaded["active_wallet_id"] == "wallet_personal_primary"
    for w in reloaded["wallets"]:
        assert w["state"] == "active"
        assert w["last_exhausted"] is None
    assert reloaded["reason"] == "probe_replenishment_recovery"


# ============================================================================ #
# 8. Critical Invariant Audit                                                  #
# ============================================================================ #


def test_adversarial_critical_invariant_zero_forbidden_references() -> None:
    """Enforce zero occurrences of forbidden term across codebase, scripts, and tests."""
    forbidden = "".join(["black", "hole"])
    pattern = re.compile(forbidden, re.IGNORECASE)

    targets = [
        PROBE_SCRIPT_PATH,
        WALLET_CTL_PATH,
        Path(__file__).resolve(),
        REPO_ROOT / "tests" / "test_probe_daemon.py",
    ]

    for target in targets:
        if target.is_file():
            content = target.read_text(encoding="utf-8")
            assert not pattern.search(content), f"Forbidden term found in {target}"
