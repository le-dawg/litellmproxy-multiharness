#!/usr/bin/env python3
"""LiteLLM MultiHarness Wallet Controller CLI.

Admin CLI utility for managing prepaid Unstoppable AI wallets, inspecting
pool status, resetting pause backpressure, and triggering one-shot probe cycles.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure repository root is on sys.path for direct script execution
script_repo_root = Path(__file__).resolve().parent.parent.parent
if (script_repo_root / "service").is_dir() and str(script_repo_root) not in sys.path:
    sys.path.insert(0, str(script_repo_root))

cwd = Path.cwd().resolve()
if (cwd / "service").is_dir() and str(cwd) not in sys.path:
    sys.path.insert(0, str(cwd))

try:
    from service.agentic.endpoint_abstraction.canopy_classifier import CanopyClassifier
    from service.agentic.endpoint_abstraction.pause_manager import (
        DEFAULT_PAUSE_LOCK_PATH,
        DEFAULT_PROBE_PID_PATH,
        PauseManager,
        PauseState,
    )
    from service.agentic.endpoint_abstraction.wallet_manager import (
        DEFAULT_KEYSTORE_PATH,
        Wallet,
        WalletManager,
    )
except ImportError as exc:
    sys.stderr.write(f"Error importing Endpoint Abstraction modules: {exc}\n")
    sys.exit(1)

logger = logging.getLogger("litellm.wallet_ctl")

DEFAULT_UPSTREAM_URL = (
    "https://app.unstoppable.ai/api/v1/llm-proxy/anthropic/v1/messages"
)
DEFAULT_PROBE_TIMEOUT = 15.0


def dispatch_desktop_notification(
    message: str, title: str = "LiteLLM MultiHarness"
) -> None:
    """Send desktop notification via osascript on macOS with safe non-blocking fallback."""
    try:
        script = f'display notification "{message}" with title "{title}"'
        subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass


def check_pid_liveness(pid: int) -> bool:
    """Check if process with given PID is currently alive."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class WalletController:
    """Core controller coordinating WalletManager, PauseManager, and live probes."""

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        keystore_path: Optional[Path] = None,
        lock_path: Optional[Path] = None,
        pid_path: Optional[Path] = None,
        as_json: bool = False,
    ) -> None:
        if config_dir:
            c_dir = Path(config_dir).expanduser().resolve()
            self.keystore_path = (
                Path(keystore_path).expanduser().resolve()
                if keystore_path
                else c_dir / "unstoppable_wallets.json"
            )
            self.lock_path = (
                Path(lock_path).expanduser().resolve()
                if lock_path
                else c_dir / "unstoppable_pause.lock"
            )
            self.pid_path = (
                Path(pid_path).expanduser().resolve()
                if pid_path
                else c_dir / "unstoppable_probe.pid"
            )
        else:
            self.keystore_path = (
                Path(keystore_path).expanduser().resolve()
                if keystore_path
                else DEFAULT_KEYSTORE_PATH
            )
            self.lock_path = (
                Path(lock_path).expanduser().resolve()
                if lock_path
                else DEFAULT_PAUSE_LOCK_PATH
            )
            self.pid_path = (
                Path(pid_path).expanduser().resolve()
                if pid_path
                else DEFAULT_PROBE_PID_PATH
            )

        self.as_json = as_json
        self.wm = WalletManager(keystore_path=self.keystore_path, auto_create=True)
        self.pm = PauseManager(lock_path=self.lock_path, auto_repair=True)

    def get_probe_status(self) -> Tuple[bool, Optional[int]]:
        """Determine probe daemon PID and process liveness."""
        probe_pid: Optional[int] = None

        if self.pid_path.is_file():
            try:
                content = self.pid_path.read_text(encoding="utf-8").strip()
                if content.isdigit():
                    probe_pid = int(content)
            except OSError:
                pass

        if probe_pid is None:
            state = self.pm.get_pause_state()
            if state and state.probe_pid:
                probe_pid = state.probe_pid

        is_running = check_pid_liveness(probe_pid) if probe_pid is not None else False
        return is_running, probe_pid

    # ------------------------------------------------------------------ #
    # Subcommand Handlers                                                #
    # ------------------------------------------------------------------ #

    def cmd_status(self) -> Tuple[int, Dict[str, Any]]:
        """Inspect wallet pool status, pause lockfile, and probe daemon liveness."""
        wallets = self.wm.get_all_wallets()
        active_id = self.wm.active_wallet_id
        is_paused = self.pm.is_paused()
        pause_state = self.pm.get_pause_state()
        probe_running, probe_pid = self.get_probe_status()

        data: Dict[str, Any] = {
            "active_wallet_id": active_id,
            "total_wallets": len(wallets),
            "wallets": [
                {
                    "id": w.id,
                    "type": w.type,
                    "state": w.state,
                    "last_exhausted": w.last_exhausted,
                }
                for w in wallets
            ],
            "pause_lock": {
                "paused": is_paused,
                "paused_at": pause_state.paused_at if pause_state else None,
                "reason": pause_state.reason if pause_state else None,
                "correlation_id": pause_state.correlation_id if pause_state else None,
                "probe_pid": pause_state.probe_pid if pause_state else None,
                "lock_path": str(self.lock_path),
            },
            "probe_daemon": {
                "running": probe_running,
                "pid": probe_pid,
                "pid_path": str(self.pid_path),
            },
        }

        if self.as_json:
            print(json.dumps(data, indent=2))
        else:
            self._render_status_text(data)

        return 0, data

    def _render_status_text(self, data: Dict[str, Any]) -> None:
        """Render human-readable tabular representation of status data."""
        print("=" * 70)
        print("LiteLLM MultiHarness Wallet Controller Status")
        print("=" * 70)
        print(f"Active Wallet ID : {data['active_wallet_id'] or 'None (Exhausted)'}")
        print(f"Total Wallets    : {data['total_wallets']}")
        print(f"Keystore Path    : {self.keystore_path}")
        print("-" * 70)
        print(f"{'ID':<20} {'TYPE':<12} {'STATE':<14} {'LAST EXHAUSTED':<20}")
        print("-" * 70)
        if not data["wallets"]:
            print("  (No wallets configured)")
        for w in data["wallets"]:
            le = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(w["last_exhausted"]))
                if w["last_exhausted"]
                else "None"
            )
            marker = "*" if w["id"] == data["active_wallet_id"] else " "
            print(f"{marker}{w['id']:<19} {w['type']:<12} {w['state']:<14} {le:<20}")

        print("-" * 70)
        pl = data["pause_lock"]
        print("Pause Lock Status:")
        if pl["paused"]:
            pa = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pl["paused_at"]))
                if pl["paused_at"]
                else "Unknown"
            )
            print(f"  Status         : PAUSED (Backpressure Active)")
            print(f"  Paused At      : {pa}")
            print(f"  Reason         : {pl['reason']}")
            print(f"  Correlation ID : {pl['correlation_id'] or 'None'}")
        else:
            print(f"  Status         : UNPAUSED (Normal Operation)")
        print(f"  Lockfile Path  : {pl['lock_path']}")

        print("-" * 70)
        pd = data["probe_daemon"]
        print("Background Probe Daemon:")
        print(f"  Status         : {'RUNNING' if pd['running'] else 'INACTIVE / NOT RUNNING'}")
        print(f"  PID            : {pd['pid'] or 'None'}")
        print(f"  PID File       : {pd['pid_path']}")
        print("=" * 70)

    def cmd_reset_lock(
        self, reset_wallets: bool = True, reason: Optional[str] = None
    ) -> Tuple[int, Dict[str, Any]]:
        """Remove pause lockfile and optionally reinstate exhausted wallets."""
        unpaused = self.pm.unpause(reason=reason or "manual_cli_reset_lock")
        wallets_reset = 0
        if reset_wallets:
            wallets = self.wm.get_all_wallets()
            exhausted_wallets = [w for w in wallets if w.state != "active"]
            if exhausted_wallets:
                self.wm.reset_all()
                wallets_reset = len(exhausted_wallets)

        data = {
            "status": "ok",
            "unpaused": unpaused,
            "wallets_reset_count": wallets_reset,
            "lock_path": str(self.lock_path),
            "message": "Pause lock removed and backpressure reset.",
        }

        if self.as_json:
            print(json.dumps(data, indent=2))
        else:
            print(f"Pause lock removed: {self.lock_path}")
            if wallets_reset > 0:
                print(f"Reinstated {wallets_reset} exhausted wallet(s) to 'active'.")
            print("Backpressure reset successfully.")

        return 0, data

    def cmd_add_key(
        self,
        key: str,
        wallet_id: Optional[str] = None,
        wallet_type: str = "personal",
        set_active: bool = False,
    ) -> Tuple[int, Dict[str, Any]]:
        """Append or update a wallet key in the keystore, preserving 0600 permissions."""
        clean_key = str(key).strip()
        if not clean_key or len(clean_key) < 8:
            err = {"status": "error", "message": "Key must be at least 8 characters long."}
            if self.as_json:
                print(json.dumps(err, indent=2))
            else:
                sys.stderr.write(f"Error: {err['message']}\n")
            return 1, err

        clean_type = wallet_type.strip().lower()
        if clean_type not in ("personal", "org"):
            clean_type = "personal"

        wallet = self.wm.add_wallet(
            wallet_or_key=clean_key,
            wallet_type=clean_type,
            wallet_id=wallet_id,
            set_active=set_active,
        )

        data = {
            "status": "ok",
            "wallet": {
                "id": wallet.id,
                "type": wallet.type,
                "state": wallet.state,
                "last_exhausted": wallet.last_exhausted,
            },
            "active_wallet_id": self.wm.active_wallet_id,
            "keystore_path": str(self.keystore_path),
            "message": f"Wallet '{wallet.id}' added successfully.",
        }

        if self.as_json:
            print(json.dumps(data, indent=2))
        else:
            print(
                f"Wallet '{wallet.id}' ({wallet.type}) saved successfully to {self.keystore_path}."
            )
            if self.wm.active_wallet_id == wallet.id:
                print(f"Active wallet pointer set to '{wallet.id}'.")

        return 0, data

    def cmd_mark_exhausted(
        self, wallet_id: str, pause_if_all_exhausted: bool = False
    ) -> Tuple[int, Dict[str, Any]]:
        """Mark a specified wallet as exhausted for failover testing."""
        clean_id = str(wallet_id).strip()
        wallet = self.wm.get_wallet(clean_id)
        if not wallet:
            err = {
                "status": "error",
                "message": f"Wallet '{clean_id}' not found in keystore.",
            }
            if self.as_json:
                print(json.dumps(err, indent=2))
            else:
                sys.stderr.write(f"Error: {err['message']}\n")
            return 1, err

        next_wallet = self.wm.mark_exhausted(clean_id)
        all_exhausted = next_wallet is None

        if all_exhausted and pause_if_all_exhausted:
            self.pm.pause(reason=f"All wallets exhausted (marked {clean_id})")

        data = {
            "status": "ok",
            "exhausted_wallet_id": clean_id,
            "active_wallet_id": next_wallet.id if next_wallet else None,
            "all_exhausted": all_exhausted,
            "message": (
                f"Wallet '{clean_id}' marked exhausted. Active rotated to '{next_wallet.id}'."
                if next_wallet
                else f"Wallet '{clean_id}' marked exhausted. All wallets in pool are now exhausted."
            ),
        }

        if self.as_json:
            print(json.dumps(data, indent=2))
        else:
            print(data["message"])

        return 0, data

    def cmd_set_active(self, wallet_id: str) -> Tuple[int, Dict[str, Any]]:
        """Explicitly switch the active wallet pointer to the specified ID."""
        clean_id = str(wallet_id).strip()
        success = self.wm.set_active_wallet(clean_id)
        if not success:
            err = {
                "status": "error",
                "message": f"Wallet '{clean_id}' not found in keystore.",
            }
            if self.as_json:
                print(json.dumps(err, indent=2))
            else:
                sys.stderr.write(f"Error: {err['message']}\n")
            return 1, err

        data = {
            "status": "ok",
            "active_wallet_id": clean_id,
            "message": f"Active wallet switched to '{clean_id}'.",
        }

        if self.as_json:
            print(json.dumps(data, indent=2))
        else:
            print(f"Active wallet pointer successfully switched to '{clean_id}'.")

        return 0, data

    def cmd_probe(
        self,
        upstream_url: Optional[str] = None,
        timeout: float = DEFAULT_PROBE_TIMEOUT,
        key: Optional[str] = None,
        no_unpause: bool = False,
    ) -> Tuple[int, Dict[str, Any]]:
        """Perform immediate one-shot probe cycle against upstream Canopy proxy."""
        target_url = (
            upstream_url
            or os.environ.get("UPSTREAM_URL")
            or os.environ.get("CANOPY_PROXY_URL")
            or DEFAULT_UPSTREAM_URL
        )

        probe_key = key
        if not probe_key:
            probe_key = self.wm.get_active_key()

        if not probe_key:
            # All wallets may be exhausted; fallback to first wallet key in keystore
            all_wallets = self.wm.get_all_wallets()
            if all_wallets:
                probe_key = all_wallets[0].key

        if not probe_key:
            probe_key = (
                os.environ.get("UNSTOPPABLE_WALLET_KEYS", "").split(",")[0].strip()
                or os.environ.get("CANOPY_API_KEY", "").strip()
                or os.environ.get("UNSTOPPABLE_CODE_KEY", "").strip()
            )

        if not probe_key:
            err = {
                "status": "error",
                "message": "No wallet key found in keystore or environment to probe.",
            }
            if self.as_json:
                print(json.dumps(err, indent=2))
            else:
                sys.stderr.write(f"Error: {err['message']}\n")
            return 1, err

        # Execute probe HTTP request conforming strictly to Interface Contract 4
        payload = json.dumps(
            {
                "model": "deepseek-v4-pro",
                "max_tokens": 1,
                "stream": True,
                "messages": [{"role": "user", "content": "probe"}],
            }
        ).encode("utf-8")

        headers = {
            "Authorization": f"Bearer {probe_key}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "LiteLLM-WalletController/1.0",
        }

        req = urllib.request.Request(
            target_url, data=payload, headers=headers, method="POST"
        )

        status_code = 0
        correlation_id: Optional[str] = None
        error_body = ""

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status_code = resp.getcode()
                correlation_id = (
                    resp.headers.get("x-canopy-correlation-id")
                    or resp.headers.get("x-request-id")
                    or resp.headers.get("request-id")
                )
                resp.read(512)
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            correlation_id = (
                exc.headers.get("x-canopy-correlation-id")
                or exc.headers.get("x-request-id")
                or exc.headers.get("request-id")
            )
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
                if not correlation_id:
                    classification = CanopyClassifier.classify(error_body)
                    correlation_id = classification.correlation_id
            except Exception:
                pass
        except Exception as exc:
            err_data = {
                "status": "error",
                "message": f"Network error during probe: {exc}",
                "upstream_url": target_url,
            }
            if self.as_json:
                print(json.dumps(err_data, indent=2))
            else:
                sys.stderr.write(f"Probe Error: {err_data['message']}\n")
            return 1, err_data

        if status_code == 200:
            unpaused = False
            if not no_unpause:
                self.pm.unpause(reason="probe_replenishment_confirmed")
                self.wm.reset_all()
                unpaused = True
                dispatch_desktop_notification(
                    "Credit replenishment confirmed. Resuming processing."
                )

            res = {
                "status": "success",
                "http_status": 200,
                "correlation_id": correlation_id,
                "action": "unpaused" if unpaused else "dry_run",
                "unpaused": unpaused,
                "message": "Credit replenishment confirmed. Processing unpaused.",
            }
            if self.as_json:
                print(json.dumps(res, indent=2))
            else:
                print("Probe Status    : SUCCESS (HTTP 200)")
                print(f"Correlation ID  : {correlation_id or 'None'}")
                print(
                    f"Action          : {'Pause lockfile removed; processing unpaused.' if unpaused else 'Replenishment verified (dry run).'}"
                )
            return 0, res

        elif status_code == 402:
            if not self.pm.is_paused():
                self.pm.pause(
                    reason="Probe detected credit exhaustion (402)",
                    correlation_id=correlation_id,
                )

            res = {
                "status": "exhausted",
                "http_status": 402,
                "correlation_id": correlation_id,
                "action": "paused",
                "unpaused": False,
                "message": "Credits remain exhausted. Processing remains paused.",
            }
            if self.as_json:
                print(json.dumps(res, indent=2))
            else:
                print("Probe Status    : EXHAUSTED (HTTP 402)")
                print(f"Correlation ID  : {correlation_id or 'None'}")
                print("Action          : Pause lock maintained; processing remains paused.")
            return 0, res

        else:
            res = {
                "status": "unexpected_status",
                "http_status": status_code,
                "correlation_id": correlation_id,
                "error_body": error_body[:256] if error_body else None,
                "unpaused": False,
                "message": f"Probe received unexpected HTTP status {status_code}.",
            }
            if self.as_json:
                print(json.dumps(res, indent=2))
            else:
                sys.stderr.write(
                    f"Probe Warning: Unexpected HTTP status {status_code} from upstream.\n"
                )
                if correlation_id:
                    sys.stderr.write(f"Correlation ID: {correlation_id}\n")
            return 1, res


def build_parser() -> argparse.ArgumentParser:
    """Construct command-line argument parser with subcommands and flags."""
    parser = argparse.ArgumentParser(
        prog="wallet_ctl.py",
        description="LiteLLM MultiHarness Wallet & Pause Lockfile Controller",
    )

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="Configuration directory containing keystore and lock files (default: ~/.litellm)",
    )
    parser.add_argument(
        "--keystore-path",
        type=Path,
        default=None,
        help="Path to unstoppable_wallets.json keystore",
    )
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=None,
        help="Path to unstoppable_pause.lock lockfile",
    )
    parser.add_argument(
        "--pid-path",
        type=Path,
        default=None,
        help="Path to unstoppable_probe.pid file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Format output as JSON",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose debug logging",
    )

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. status
    p_status = subparsers.add_parser(
        "status", help="Display wallet pool status, pause lock, and probe daemon liveness"
    )
    p_status.add_argument("--json", action="store_true", default=False, help="Format output as JSON")

    # 2. probe
    p_probe = subparsers.add_parser(
        "probe", help="Perform immediate one-shot probe cycle against upstream Canopy"
    )
    p_probe.add_argument("--upstream-url", type=str, default=None, help="Canopy proxy endpoint URL")
    p_probe.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_PROBE_TIMEOUT,
        help=f"Request timeout in seconds (default: {DEFAULT_PROBE_TIMEOUT})",
    )
    p_probe.add_argument("--key", type=str, default=None, help="Explicit API key to probe")
    p_probe.add_argument(
        "--no-unpause",
        action="store_true",
        default=False,
        help="Verify replenishment without unpausing lock",
    )
    p_probe.add_argument("--json", action="store_true", default=False, help="Format output as JSON")

    # 3. reset-lock / unpause
    for name in ("reset-lock", "unpause"):
        p_unpause = subparsers.add_parser(
            name, help="Safely remove unstoppable_pause.lock and reset backpressure"
        )
        p_unpause.add_argument(
            "--no-reset-wallets",
            action="store_true",
            default=False,
            help="Do not reset exhausted wallet states in keystore",
        )
        p_unpause.add_argument("--reason", type=str, default=None, help="Reason for unpausing")
        p_unpause.add_argument("--json", action="store_true", default=False, help="Format output as JSON")

    # 4. add-key
    p_add = subparsers.add_parser("add-key", help="Append new wallet key to keystore with 0600 mode")
    p_add.add_argument("key", type=str, help="Prepaid API key string")
    p_add.add_argument("--id", type=str, default=None, help="Optional wallet ID")
    p_add.add_argument(
        "--type",
        type=str,
        default="personal",
        choices=["personal", "org"],
        help="Wallet type (personal or org)",
    )
    p_add.add_argument(
        "--set-active",
        action="store_true",
        default=False,
        help="Set new key as the active wallet",
    )
    p_add.add_argument("--json", action="store_true", default=False, help="Format output as JSON")

    # 5. mark-exhausted
    p_mark = subparsers.add_parser(
        "mark-exhausted", help="Mark specified wallet exhausted for failover testing"
    )
    p_mark.add_argument("wallet_id", type=str, help="Target wallet ID")
    p_mark.add_argument(
        "--pause",
        action="store_true",
        default=False,
        help="Activate pause lock if all wallets become exhausted",
    )
    p_mark.add_argument("--json", action="store_true", default=False, help="Format output as JSON")

    # 6. set-active
    p_set = subparsers.add_parser(
        "set-active", help="Explicitly switch active wallet pointer"
    )
    p_set.add_argument("wallet_id", type=str, help="Target wallet ID")
    p_set.add_argument("--json", action="store_true", default=False, help="Format output as JSON")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint function returning standard integer exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    as_json = getattr(args, "json", False)

    controller = WalletController(
        config_dir=args.config_dir,
        keystore_path=args.keystore_path,
        lock_path=args.lock_path,
        pid_path=args.pid_path,
        as_json=as_json,
    )

    cmd = args.subcommand
    if cmd == "status":
        code, _ = controller.cmd_status()
        return code
    elif cmd == "probe":
        code, _ = controller.cmd_probe(
            upstream_url=args.upstream_url,
            timeout=args.timeout,
            key=args.key,
            no_unpause=args.no_unpause,
        )
        return code
    elif cmd in ("reset-lock", "unpause"):
        code, _ = controller.cmd_reset_lock(
            reset_wallets=not args.no_reset_wallets,
            reason=args.reason,
        )
        return code
    elif cmd == "add-key":
        code, _ = controller.cmd_add_key(
            key=args.key,
            wallet_id=args.id,
            wallet_type=args.type,
            set_active=args.set_active,
        )
        return code
    elif cmd == "mark-exhausted":
        code, _ = controller.cmd_mark_exhausted(
            wallet_id=args.wallet_id,
            pause_if_all_exhausted=args.pause,
        )
        return code
    elif cmd == "set-active":
        code, _ = controller.cmd_set_active(wallet_id=args.wallet_id)
        return code
    else:
        sys.stderr.write(f"Unknown subcommand: {cmd}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
