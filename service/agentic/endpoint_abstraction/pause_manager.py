"""Fast-Fail Backpressure Pause Manager and Lockfile Lifecycle Controller.

Provides thread-safe atomic management of ~/.litellm/unstoppable_pause.lock,
stat-signature cached fast-fail checks, automatic corruption quarantine and recovery,
and probe daemon PID monitoring for LiteLLM MultiHarness.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

logger = logging.getLogger("litellm.endpoint_abstraction.pause_manager")

# Default filesystem locations
DEFAULT_PAUSE_LOCK_PATH = Path.home() / ".litellm" / "unstoppable_pause.lock"
DEFAULT_PROBE_PID_PATH = Path.home() / ".litellm" / "unstoppable_probe.pid"
DEFAULT_PROBE_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "bin" / "unstoppable_wallet_probe.sh"
)

LOCK_PERMISSIONS = 0o600  # Strict user read/write only (-rw-------)


@dataclass
class PauseState:
    """Immutable representation of the pause lockfile payload."""

    paused: bool
    paused_at: float
    reason: str
    correlation_id: Optional[str] = None
    probe_pid: Optional[int] = None

    def __post_init__(self) -> None:
        self.paused = bool(self.paused)
        try:
            self.paused_at = float(self.paused_at)
        except (ValueError, TypeError):
            self.paused_at = time.time()

        self.reason = str(self.reason).strip() if self.reason else "All wallets exhausted"
        self.correlation_id = str(self.correlation_id).strip() if self.correlation_id else None

        if self.probe_pid is not None:
            try:
                pid_int = int(self.probe_pid)
                self.probe_pid = pid_int if pid_int > 0 else None
            except (ValueError, TypeError):
                self.probe_pid = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state to standard dictionary matching Interface Contract 2."""
        return {
            "paused": self.paused,
            "paused_at": self.paused_at,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
            "probe_pid": self.probe_pid,
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize state to formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PauseState":
        """Construct and validate PauseState from dictionary."""
        if not isinstance(data, dict):
            raise TypeError(f"Pause payload must be a dict, got {type(data).__name__}")
        return cls(
            paused=bool(data.get("paused", True)),
            paused_at=data.get("paused_at", time.time()),
            reason=data.get("reason", "All wallets exhausted"),
            correlation_id=data.get("correlation_id"),
            probe_pid=data.get("probe_pid"),
        )

    @classmethod
    def from_json(cls, text: str) -> "PauseState":
        """Parse JSON text into validated PauseState."""
        payload = json.loads(text)
        return cls.from_dict(payload)


class PauseManager:
    """Thread-safe and cross-process pause lockfile manager with fast-fail caching."""

    def __init__(
        self,
        lock_path: Optional[Union[str, Path]] = None,
        auto_repair: bool = True,
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        if lock_path:
            self.lock_path = Path(lock_path).expanduser().resolve()
        else:
            env_path = os.environ.get("UNSTOPPABLE_PAUSE_LOCK_PATH")
            self.lock_path = (
                Path(env_path).expanduser().resolve()
                if env_path
                else DEFAULT_PAUSE_LOCK_PATH
            )

        self.auto_repair = auto_repair
        self._time_fn = time_fn
        self._lock = threading.RLock()

        # In-memory fast-fail cache
        self._is_paused: bool = False
        self._cached_state: Optional[PauseState] = None
        self._cached_stat_signature: Optional[Tuple[int, int, int]] = None

        with self._lock:
            self._sync_from_disk_if_changed(force=True)

    def _get_now(self) -> float:
        """Return current timestamp respecting time_fn if injected."""
        return self._time_fn() if self._time_fn is not None else time.time()

    def _sync_from_disk_if_changed(self, force: bool = False) -> bool:
        """Check filesystem stat signature and reload lockfile if modified."""
        try:
            stat_res = self.lock_path.stat()
        except FileNotFoundError:
            if self._cached_stat_signature is not None:
                logger.info(
                    "Pause lockfile %s removed externally. Unpausing proxy.",
                    self.lock_path,
                )
                self._is_paused = False
                self._cached_state = None
                self._cached_stat_signature = None
            return self._is_paused
        except OSError as exc:
            logger.warning(
                "Error stating pause lockfile %s: %s. Retaining cached state.",
                self.lock_path,
                exc,
            )
            return self._is_paused

        # File exists; compute composite stat signature
        current_sig = (stat_res.st_mtime_ns, stat_res.st_size, stat_res.st_ino)
        if (
            not force
            and self._cached_stat_signature is not None
            and current_sig == self._cached_stat_signature
        ):
            return self._is_paused

        # Read and parse lockfile
        return self._read_and_parse_lock(stat_res, current_sig)

    def _read_and_parse_lock(
        self, stat_res: os.stat_result, current_sig: Tuple[int, int, int]
    ) -> bool:
        """Read and validate lockfile content with corruption recovery."""
        if stat_res.st_size == 0:
            logger.warning(
                "Pause lockfile %s is 0 bytes (corrupt/empty).", self.lock_path
            )
            if self.auto_repair:
                return self.recover_corrupted_lock(reason="Recovered from 0-byte lockfile")
            self._is_paused = True
            self._cached_state = PauseState(
                paused=True,
                paused_at=self._get_now(),
                reason="Corrupt empty lockfile detected",
            )
            self._cached_stat_signature = current_sig
            return True

        try:
            text = self.lock_path.read_text(encoding="utf-8").strip()
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError(f"Payload root is not a dict: {type(payload)}")

            state = PauseState.from_dict(payload)
            self._is_paused = bool(state.paused)
            self._cached_state = state if self._is_paused else None
            self._cached_stat_signature = current_sig
            return self._is_paused
        except Exception as exc:
            logger.warning("Corrupted pause lockfile %s: %s", self.lock_path, exc)
            if self.auto_repair:
                return self.recover_corrupted_lock(
                    reason=f"Recovered from invalid JSON ({exc})"
                )
            self._is_paused = True
            self._cached_state = PauseState(
                paused=True,
                paused_at=self._get_now(),
                reason=f"Corrupted lockfile ({exc})",
            )
            self._cached_stat_signature = current_sig
            return True

    def is_paused(self) -> bool:
        """Fast-fail check returning True if processing is paused.

        Uses stat signature caching to avoid repeated file reads and JSON deserialization.
        """
        try:
            stat_res = self.lock_path.stat()
        except FileNotFoundError:
            if self._cached_stat_signature is not None:
                with self._lock:
                    if self._cached_stat_signature is not None:
                        self._is_paused = False
                        self._cached_state = None
                        self._cached_stat_signature = None
            return self._is_paused
        except OSError:
            with self._lock:
                return self._is_paused

        current_sig = (stat_res.st_mtime_ns, stat_res.st_size, stat_res.st_ino)
        if (
            self._cached_stat_signature is not None
            and current_sig == self._cached_stat_signature
        ):
            return self._is_paused

        with self._lock:
            return self._sync_from_disk_if_changed(force=True)

    def get_pause_state(self) -> Optional[PauseState]:
        """Return the current PauseState if paused, else None."""
        if not self.is_paused():
            return None
        with self._lock:
            return self._cached_state

    def pause(
        self,
        reason: str = "All wallets exhausted (personal_wallet_empty)",
        correlation_id: Optional[str] = None,
        probe_pid: Optional[int] = None,
        paused_at: Optional[float] = None,
    ) -> bool:
        """Atomically create the pause lockfile with 0600 permissions."""
        now = paused_at if paused_at is not None else self._get_now()
        state = PauseState(
            paused=True,
            paused_at=now,
            reason=reason,
            correlation_id=correlation_id,
            probe_pid=probe_pid,
        )

        with self._lock:
            tmp_path: Optional[Path] = None
            fd: Optional[int] = None
            try:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_filename = (
                    f"{self.lock_path.name}.{os.getpid()}."
                    f"{threading.get_ident()}.{time.time_ns()}.tmp"
                )
                tmp_path = self.lock_path.parent / tmp_filename

                fd = os.open(
                    str(tmp_path),
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    LOCK_PERMISSIONS,
                )
                with open(fd, "w", encoding="utf-8", closefd=True) as f:
                    fd = None  # Transferred to file object
                    f.write(state.to_json(indent=2))
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())

                try:
                    os.chmod(tmp_path, LOCK_PERMISSIONS)
                except OSError:
                    pass

                os.replace(tmp_path, self.lock_path)
                tmp_path = None  # Successfully moved

                stat_res = self.lock_path.stat()
                self._cached_stat_signature = (
                    stat_res.st_mtime_ns,
                    stat_res.st_size,
                    stat_res.st_ino,
                )
                self._is_paused = True
                self._cached_state = state
                logger.warning(
                    "MultiHarness PAUSED: %s (correlation_id=%s, probe_pid=%s)",
                    reason,
                    correlation_id,
                    probe_pid,
                )
                return True
            except OSError as exc:
                logger.error(
                    "Failed to write pause lockfile %s: %s. Preserving in-memory pause state.",
                    self.lock_path,
                    exc,
                )
                self._is_paused = True
                self._cached_state = state
                return False
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if tmp_path is not None and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass

    def unpause(self, reason: Optional[str] = None) -> bool:
        """Atomically remove the pause lockfile and reset in-memory state."""
        with self._lock:
            try:
                if self.lock_path.exists():
                    self.lock_path.unlink()
            except OSError as exc:
                logger.warning(
                    "Failed to unlink pause lockfile %s: %s", self.lock_path, exc
                )

            self._is_paused = False
            self._cached_state = None
            self._cached_stat_signature = None
            logger.info(
                "MultiHarness UNPAUSED: %s", reason or "manual/probe unpause"
            )
            return True

    def reset(self) -> bool:
        """Convenience alias for unpause()."""
        return self.unpause(reason="reset")

    def update_probe_pid(self, pid: int) -> bool:
        """Atomically update probe_pid in lockfile if currently paused."""
        with self._lock:
            if not self.is_paused() or self._cached_state is None:
                return False
            curr = self._cached_state
            return self.pause(
                reason=curr.reason,
                correlation_id=curr.correlation_id,
                probe_pid=pid,
                paused_at=curr.paused_at,
            )

    def is_probe_running(self) -> bool:
        """Check whether the recorded probe daemon PID is currently active."""
        state = self.get_pause_state()
        if state is None or state.probe_pid is None:
            return False

        pid = state.probe_pid
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # Process exists, owned by another user/session
        except OSError:
            return False

    def ensure_probe_daemon_running(
        self, script_path: Optional[Union[str, Path]] = None
    ) -> Optional[int]:
        """Verify probe daemon is running, spawning it detached if absent."""
        with self._lock:
            if not self.is_paused():
                return None

            if (
                self.is_probe_running()
                and self._cached_state
                and self._cached_state.probe_pid
            ):
                return self._cached_state.probe_pid

            target_script = (
                Path(script_path) if script_path else DEFAULT_PROBE_SCRIPT_PATH
            )
            if not target_script.is_file():
                logger.warning(
                    "Probe script not found at %s. Cannot spawn probe daemon.",
                    target_script,
                )
                return None

            try:
                proc = subprocess.Popen(
                    ["/bin/bash", str(target_script)],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
                pid = proc.pid
                self.update_probe_pid(pid)
                logger.info(
                    "Spawned background probe daemon (PID %d) for script %s",
                    pid,
                    target_script,
                )
                return pid
            except OSError as exc:
                logger.error(
                    "Failed to spawn background probe daemon %s: %s",
                    target_script,
                    exc,
                )
                return None

    def recover_corrupted_lock(
        self, reason: str = "Corrupted lockfile recovered"
    ) -> bool:
        """Quarantine damaged lockfile and atomically recreate valid pause state."""
        with self._lock:
            try:
                if self.lock_path.exists():
                    corrupt_backup = self.lock_path.with_name(
                        f"{self.lock_path.name}.corrupt.{time.time_ns()}"
                    )
                    os.replace(self.lock_path, corrupt_backup)
                    logger.info("Quarantined corrupt pause lock to %s", corrupt_backup)
            except OSError as exc:
                logger.warning(
                    "Could not quarantine corrupt lockfile %s: %s",
                    self.lock_path,
                    exc,
                )

            return self.pause(reason=reason)

    def check_pause(self) -> None:
        """Fast-fail check for LiteLLM hooks, raising HTTPException(402) if paused."""
        if self.is_paused():
            state = self.get_pause_state()
            try:
                from service.agentic.endpoint_abstraction.error_enricher import (
                    ErrorEnricher,
                )

                raise ErrorEnricher.create_http_exception(
                    correlation_id=state.correlation_id if state else None,
                    status="paused",
                )
            except ImportError:
                from fastapi import HTTPException

                detail = {
                    "error": {
                        "message": (
                            "[Unstoppable AI Resource Depleted] All configured prepaid wallets have zero credit. "
                            "Processing is paused while the background probe monitors fund replenishment."
                        ),
                        "type": "payment_required",
                        "code": 402,
                        "canopy_correlation_id": (
                            state.correlation_id if state else None
                        ),
                        "status": "paused",
                        "resume_instruction": (
                            "Run 'claude --resume' once funds are replenished or after receiving the desktop notification."
                        ),
                    }
                }
                raise HTTPException(status_code=402, detail=detail)
