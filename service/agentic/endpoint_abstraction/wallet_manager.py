"""Multi-Wallet Keystore and State Persistence Manager for LiteLLM MultiHarness.

Provides thread-safe multi-wallet credential rotation, dynamic mtime cache
synchronization, atomic JSON persistence, and automatic cooldown recovery
for Upstream Canopy Proxy routing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger("litellm.endpoint_abstraction.wallet_manager")

# Default filesystem locations and parameters
DEFAULT_KEYSTORE_PATH = Path.home() / ".litellm" / "unstoppable_wallets.json"
DEFAULT_COOLDOWN_SECONDS = 3600.0  # 1 hour

VALID_WALLET_TYPES: Set[str] = {"personal", "org"}
VALID_WALLET_STATES: Set[str] = {"active", "exhausted", "cooldown"}


@dataclass
class Wallet:
    """Individual wallet credential and lifecycle state model."""

    id: str
    key: str
    type: str = "personal"
    state: str = "active"
    last_exhausted: Optional[float] = None

    def __post_init__(self) -> None:
        self.id = str(self.id).strip()
        if not self.id:
            raise ValueError("Wallet id must be a non-empty string.")

        self.key = str(self.key).strip()
        if not self.key or len(self.key) < 8:
            raise ValueError(f"Wallet key for '{self.id}' must be a non-empty string of at least 8 characters.")

        clean_type = str(self.type).strip().lower()
        self.type = clean_type if clean_type in VALID_WALLET_TYPES else "personal"

        clean_state = str(self.state).strip().lower()
        self.state = clean_state if clean_state in VALID_WALLET_STATES else "active"

        if self.last_exhausted is not None:
            try:
                self.last_exhausted = float(self.last_exhausted)
            except (ValueError, TypeError):
                self.last_exhausted = None

    def is_active(self) -> bool:
        """Return True if wallet is currently in 'active' state."""
        return self.state == "active"

    def is_healthy(self, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS, now: Optional[float] = None) -> bool:
        """Return True if wallet is active or has exceeded its cooldown duration."""
        if self.state == "active":
            return True
        if self.last_exhausted is None:
            return False
        current_time = time.time() if now is None else now
        return (current_time - self.last_exhausted) >= max(0.0, cooldown_seconds)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize wallet model to a dictionary representation."""
        return {
            "id": self.id,
            "key": self.key,
            "type": self.type,
            "state": self.state,
            "last_exhausted": self.last_exhausted,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Wallet":
        """Construct and validate a Wallet instance from a dictionary."""
        if not isinstance(data, dict):
            raise TypeError(f"Wallet data must be a dict, got {type(data).__name__}")
        return cls(
            id=data.get("id", ""),
            key=data.get("key", ""),
            type=data.get("type", "personal"),
            state=data.get("state", "active"),
            last_exhausted=data.get("last_exhausted"),
        )


@dataclass
class KeystoreData:
    """Keystore container model holding all wallets and active state."""

    wallets: List[Wallet]
    active_wallet_id: Optional[str]
    updated_at: float
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize container to dictionary."""
        d: Dict[str, Any] = {
            "wallets": [w.to_dict() for w in self.wallets],
            "active_wallet_id": self.active_wallet_id,
            "updated_at": self.updated_at,
        }
        if self.reason:
            d["reason"] = self.reason
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KeystoreData":
        """Construct KeystoreData container from dictionary."""
        if not isinstance(data, dict):
            raise TypeError(f"Keystore data must be a dict, got {type(data).__name__}")
        raw_wallets = data.get("wallets")
        wallets: List[Wallet] = []
        if isinstance(raw_wallets, list):
            for item in raw_wallets:
                if isinstance(item, dict):
                    try:
                        wallets.append(Wallet.from_dict(item))
                    except (ValueError, TypeError) as exc:
                        logger.warning("Skipping invalid wallet record %s: %s", item, exc)

        active_wallet_id = data.get("active_wallet_id")
        if active_wallet_id is not None:
            active_wallet_id = str(active_wallet_id)

        try:
            updated_at = float(data.get("updated_at", time.time()))
        except (ValueError, TypeError):
            updated_at = time.time()

        reason = data.get("reason")
        return cls(
            wallets=wallets,
            active_wallet_id=active_wallet_id,
            updated_at=updated_at,
            reason=str(reason) if reason else None,
        )


class WalletManager:
    """Thread-safe multi-wallet keystore manager with dynamic mtime reloading."""

    def __init__(
        self,
        keystore_path: Optional[Union[str, Path]] = None,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        env_fallback: bool = True,
        auto_create: bool = True,
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        if keystore_path:
            self.keystore_path = Path(keystore_path).expanduser().resolve()
        else:
            env_path = os.environ.get("UNSTOPPABLE_KEYSTORE_PATH")
            self.keystore_path = Path(env_path).expanduser().resolve() if env_path else DEFAULT_KEYSTORE_PATH

        env_cooldown = os.environ.get("UNSTOPPABLE_WALLET_COOLDOWN_SECONDS")
        if env_cooldown:
            try:
                self.cooldown_seconds = float(env_cooldown)
            except ValueError:
                self.cooldown_seconds = cooldown_seconds
        else:
            self.cooldown_seconds = cooldown_seconds

        self.env_fallback = env_fallback
        self.auto_create = auto_create
        self._time_fn = time_fn

        self._lock = threading.RLock()
        self._wallets: Dict[str, Wallet] = {}
        self._wallet_order: List[str] = []
        self._active_wallet_id: Optional[str] = None

        # Composite cache key: (mtime_ns, size, ino)
        self._cached_stat_signature: Optional[Tuple[int, int, int]] = None

        with self._lock:
            self._initialize_keystore()

    def _get_now(self) -> float:
        """Return current timestamp respecting time_fn if configured."""
        return self._time_fn() if self._time_fn is not None else time.time()

    @property
    def active_wallet_id(self) -> Optional[str]:
        """Return the current active wallet ID."""
        with self._lock:
            return self._active_wallet_id

    def _initialize_keystore(self) -> None:
        """Perform initial load from disk or environment fallback."""
        loaded = self._load_from_disk()
        if not loaded and self.env_fallback:
            self._load_from_env_fallback()
            if self.auto_create and self._wallets:
                self._persist_to_disk(reason="initialized_from_env_fallback")

    def _load_from_disk(self) -> bool:
        """Attempt to load and parse keystore file from disk. Returns True on success."""
        if not self.keystore_path.is_file():
            return False

        try:
            stat_res = self.keystore_path.stat()
            text = self.keystore_path.read_text(encoding="utf-8").strip()
            if not text:
                logger.warning("Keystore file %s is empty. Retaining cached memory state.", self.keystore_path)
                return False

            payload = json.loads(text)
            if not isinstance(payload, dict):
                logger.warning("Keystore payload is not a dict: %s", type(payload))
                return False

            data = KeystoreData.from_dict(payload)

            new_wallets: Dict[str, Wallet] = {}
            new_order: List[str] = []
            for wallet in data.wallets:
                new_wallets[wallet.id] = wallet
                if wallet.id not in new_order:
                    new_order.append(wallet.id)

            new_active_wallet_id = data.active_wallet_id
            if new_active_wallet_id and new_active_wallet_id not in new_wallets:
                new_active_wallet_id = new_order[0] if new_order else None

            # Atomic commit to in-memory state
            self._wallets = new_wallets
            self._wallet_order = new_order
            self._active_wallet_id = new_active_wallet_id

            self._cached_stat_signature = (stat_res.st_mtime_ns, stat_res.st_size, stat_res.st_ino)
            return True
        except Exception as exc:
            logger.warning("Failed to load keystore from %s: %s. Retaining cached memory state.", self.keystore_path, exc)
            return False

    def _load_from_env_fallback(self) -> None:
        """Load initial wallets from UNSTOPPABLE_WALLET_KEYS or legacy environment keys."""
        env_keys_raw = os.environ.get("UNSTOPPABLE_WALLET_KEYS", "").strip()
        parsed_keys: List[str] = []

        if env_keys_raw:
            tokens = re.split(r"[,;\n]+", env_keys_raw)
            for token in tokens:
                clean = token.strip()
                if clean and len(clean) >= 8 and clean not in parsed_keys:
                    parsed_keys.append(clean)

        if not parsed_keys:
            legacy_key = (
                os.environ.get("UNSTOPPABLE_CODE_KEY", "").strip()
                or os.environ.get("UNSTOPPABLE_ANTHROPIC_KEY", "").strip()
            )
            if legacy_key and len(legacy_key) >= 8:
                parsed_keys.append(legacy_key)

        if not parsed_keys:
            return

        self._wallets.clear()
        self._wallet_order.clear()

        for idx, key in enumerate(parsed_keys):
            wallet_id = f"wallet_{idx + 1}"
            wallet = Wallet(
                id=wallet_id,
                key=key,
                type="personal" if idx == 0 else "org",
                state="active",
                last_exhausted=None,
            )
            self._wallets[wallet.id] = wallet
            self._wallet_order.append(wallet.id)

        self._active_wallet_id = self._wallet_order[0] if self._wallet_order else None

    def _sync_from_disk_if_changed(self, force: bool = False) -> bool:
        """Check stat signature and reload if keystore was updated externally."""
        if not self.keystore_path.is_file():
            return False

        try:
            stat_res = self.keystore_path.stat()
            signature = (stat_res.st_mtime_ns, stat_res.st_size, stat_res.st_ino)
            if not force and self._cached_stat_signature is not None and signature == self._cached_stat_signature:
                return False
            return self._load_from_disk()
        except OSError:
            return False

    def _apply_cooldowns_locked(self, now: Optional[float] = None) -> int:
        """Evaluate cooldown recovery for all exhausted wallets while holding self._lock."""
        current_time = self._get_now() if now is None else now
        recovered_count = 0

        for wallet in self._wallets.values():
            if wallet.state in ("exhausted", "cooldown") and wallet.is_healthy(self.cooldown_seconds, current_time):
                wallet.state = "active"
                wallet.last_exhausted = None
                recovered_count += 1
                logger.info("Wallet '%s' recovered from cooldown to 'active'", wallet.id)

        # If current active wallet is None or exhausted, point to first recovered/healthy wallet
        if self._active_wallet_id is None or (
            self._active_wallet_id in self._wallets
            and self._wallets[self._active_wallet_id].state != "active"
        ):
            healthy = self._find_first_healthy_wallet_locked()
            if healthy:
                self._active_wallet_id = healthy.id

        return recovered_count

    def _find_first_healthy_wallet_locked(self) -> Optional[Wallet]:
        """Find first healthy wallet prioritizing personal, then org."""
        active_wallets = [w for w in self._wallets.values() if w.state == "active"]
        if not active_wallets:
            return None

        personal_active = [w for w in active_wallets if w.type == "personal"]
        candidate_pool = personal_active if personal_active else active_wallets

        for wallet_id in self._wallet_order:
            wallet = self._wallets.get(wallet_id)
            if wallet and wallet in candidate_pool:
                return wallet
        return candidate_pool[0]

    def _persist_to_disk(self, reason: Optional[str] = None) -> bool:
        """Atomically persist keystore to disk with permissions 0600.

        Resilient to filesystem errors (PermissionError, ENOSPC, EROFS);
        catches OSError, logs a warning, cleans up any partial temporary files,
        and safely preserves updated in-memory state.
        """
        tmp_path: Optional[Path] = None
        fd: Optional[int] = None

        wallets_to_serialize = [self._wallets[wid] for wid in self._wallet_order if wid in self._wallets]
        for wid, w in self._wallets.items():
            if wid not in self._wallet_order:
                wallets_to_serialize.append(w)

        data = KeystoreData(
            wallets=wallets_to_serialize,
            active_wallet_id=self._active_wallet_id,
            updated_at=self._get_now(),
            reason=reason,
        )

        try:
            self.keystore_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_filename = f"{self.keystore_path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
            tmp_path = self.keystore_path.parent / tmp_filename

            fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with open(fd, "w", encoding="utf-8", closefd=True) as f:
                fd = None  # Transferred ownership to file object
                json.dump(data.to_dict(), f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())

            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass

            os.replace(tmp_path, self.keystore_path)
            tmp_path = None  # Successfully committed, do not unlink

            stat_res = self.keystore_path.stat()
            self._cached_stat_signature = (stat_res.st_mtime_ns, stat_res.st_size, stat_res.st_ino)
            return True
        except OSError as exc:
            logger.warning(
                "Failed to persist keystore to %s: %s. Preserving in-memory state.",
                self.keystore_path,
                exc,
            )
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

    # ------------------------------------------------------------------ #
    # Core Public Interface                                              #
    # ------------------------------------------------------------------ #

    def get_active_wallet(self) -> Optional[Wallet]:
        """Return the currently active healthy Wallet, or None if all are exhausted."""
        with self._lock:
            self._sync_from_disk_if_changed()
            recovered = self._apply_cooldowns_locked()
            if recovered > 0 and self.auto_create:
                self._persist_to_disk(reason="cooldown_expiration_reinstatement")

            if self._active_wallet_id and self._active_wallet_id in self._wallets:
                candidate = self._wallets[self._active_wallet_id]
                if candidate.state == "active":
                    return candidate

            healthy = self._find_first_healthy_wallet_locked()
            if healthy:
                self._active_wallet_id = healthy.id
                if self.auto_create:
                    self._persist_to_disk(reason="active_wallet_selection")
                return healthy

            self._active_wallet_id = None
            return None

    def get_active_key(self) -> Optional[str]:
        """Return the active healthy API key string, or None if all wallets exhausted."""
        wallet = self.get_active_wallet()
        return wallet.key if wallet else None

    def mark_exhausted(self, wallet_id: Optional[str] = None) -> Optional[Wallet]:
        """Mark a wallet exhausted and advance active pointer to next healthy wallet.

        Args:
            wallet_id: Specific wallet ID to mark. If None, marks the currently active wallet.

        Returns:
            The newly active healthy Wallet, or None if all wallets are now exhausted.
        """
        with self._lock:
            self._sync_from_disk_if_changed()
            self._apply_cooldowns_locked()

            target_id = wallet_id or self._active_wallet_id
            if not target_id or target_id not in self._wallets:
                logger.warning("mark_exhausted called for unknown or missing wallet ID: %s", target_id)
                return self.get_active_wallet()

            exhausted_wallet = self._wallets[target_id]
            exhausted_wallet.state = "exhausted"
            exhausted_wallet.last_exhausted = self._get_now()
            logger.warning("Wallet '%s' marked exhausted at %s", target_id, exhausted_wallet.last_exhausted)

            # Idempotency guard: If another concurrent request already rotated the active pointer
            # away from target_id to another active wallet, do not double rotate.
            if wallet_id is not None and self._active_wallet_id != wallet_id and self._active_wallet_id is not None:
                current_active = self._wallets.get(self._active_wallet_id)
                if current_active and current_active.state == "active":
                    if self.auto_create:
                        self._persist_to_disk(reason=f"wallet_{target_id}_exhausted")
                    return current_active

            # Select candidate pool: personal first, then org
            active_wallets = [w for w in self._wallets.values() if w.state == "active"]
            personal_active = [w for w in active_wallets if w.type == "personal"]
            candidate_pool = personal_active if personal_active else active_wallets

            next_active: Optional[Wallet] = None
            if candidate_pool and self._wallet_order:
                start_idx = self._wallet_order.index(target_id) if target_id in self._wallet_order else 0
                total = len(self._wallet_order)
                for i in range(1, total + 1):
                    cand_id = self._wallet_order[(start_idx + i) % total]
                    cand = self._wallets.get(cand_id)
                    if cand and cand in candidate_pool:
                        next_active = cand
                        break
            if not next_active and candidate_pool:
                next_active = candidate_pool[0]

            if next_active:
                self._active_wallet_id = next_active.id
                logger.info("Rotated active wallet to '%s'", next_active.id)
            else:
                self._active_wallet_id = None
                logger.critical("All wallets exhausted in keystore!")

            if self.auto_create:
                self._persist_to_disk(reason=f"wallet_{target_id}_exhausted")
            return next_active

    def has_active_wallet(self) -> bool:
        """Return True if there is at least one active healthy wallet in the pool."""
        return self.get_active_wallet() is not None

    def reset_all(self) -> None:
        """Reset all wallets in pool back to 'active' state and select the first wallet."""
        with self._lock:
            self._sync_from_disk_if_changed()
            for wallet in self._wallets.values():
                wallet.state = "active"
                wallet.last_exhausted = None

            if self._wallet_order:
                personal = [
                    w_id for w_id in self._wallet_order
                    if w_id in self._wallets and self._wallets[w_id].type == "personal"
                ]
                valid_order = [w_id for w_id in self._wallet_order if w_id in self._wallets]
                self._active_wallet_id = personal[0] if personal else (valid_order[0] if valid_order else None)
            else:
                self._active_wallet_id = None

            if self.auto_create:
                self._persist_to_disk(reason="manual_reset_all")
            logger.info("Reset all %d wallets to 'active'", len(self._wallets))

    def get_all_wallets(self) -> List[Wallet]:
        """Return a snapshot list of all wallets currently in the keystore."""
        with self._lock:
            self._sync_from_disk_if_changed()
            self._apply_cooldowns_locked()
            return [Wallet.from_dict(w.to_dict()) for w in self._wallets.values()]

    def get_wallet(self, wallet_id: str) -> Optional[Wallet]:
        """Retrieve a specific wallet by its ID."""
        with self._lock:
            self._sync_from_disk_if_changed()
            wallet = self._wallets.get(wallet_id)
            return Wallet.from_dict(wallet.to_dict()) if wallet else None

    def get_wallet_by_key(self, key: str) -> Optional[Wallet]:
        """Retrieve a specific wallet by its API key string."""
        with self._lock:
            self._sync_from_disk_if_changed()
            clean_key = str(key).strip()
            for wallet in self._wallets.values():
                if wallet.key == clean_key:
                    return Wallet.from_dict(wallet.to_dict())
            return None

    def add_wallet(
        self,
        wallet_or_key: Union[Wallet, str],
        wallet_type: str = "personal",
        wallet_id: Optional[str] = None,
        state: str = "active",
        set_active: bool = False,
    ) -> Wallet:
        """Add or update a wallet in the pool and persist to disk.

        Supports both add_wallet(Wallet(...)) and add_wallet("csk_key...", wallet_id="...").
        """
        with self._lock:
            self._sync_from_disk_if_changed()

            if isinstance(wallet_or_key, Wallet):
                wallet = Wallet(
                    id=wallet_or_key.id,
                    key=wallet_or_key.key,
                    type=wallet_or_key.type,
                    state=wallet_or_key.state,
                    last_exhausted=wallet_or_key.last_exhausted,
                )
            else:
                key = str(wallet_or_key).strip()
                target_id = wallet_id or f"wallet_{len(self._wallets) + 1}"
                wallet = Wallet(id=target_id, key=key, type=wallet_type, state=state)

            self._wallets[wallet.id] = wallet
            if wallet.id not in self._wallet_order:
                self._wallet_order.append(wallet.id)

            if set_active or not self._active_wallet_id or self._active_wallet_id not in self._wallets:
                self._active_wallet_id = wallet.id

            if self.auto_create:
                self._persist_to_disk(reason=f"wallet_{wallet.id}_added")
            return wallet

    def remove_wallet(self, wallet_id: str) -> bool:
        """Remove a wallet from the keystore by ID. Returns True if removed."""
        with self._lock:
            self._sync_from_disk_if_changed()
            if wallet_id not in self._wallets:
                if wallet_id in self._wallet_order:
                    self._wallet_order = [w for w in self._wallet_order if w != wallet_id]
                return False

            del self._wallets[wallet_id]
            self._wallet_order = [w for w in self._wallet_order if w != wallet_id]

            if self._active_wallet_id == wallet_id or (self._active_wallet_id and self._active_wallet_id not in self._wallets):
                healthy = self._find_first_healthy_wallet_locked()
                self._active_wallet_id = healthy.id if healthy else None

            if self.auto_create:
                self._persist_to_disk(reason=f"wallet_{wallet_id}_removed")
            return True

    def reinstate_wallet(self, wallet_id: str) -> bool:
        """Manually reinstate a specific wallet back to active state."""
        with self._lock:
            self._sync_from_disk_if_changed()
            wallet = self._wallets.get(wallet_id)
            if not wallet:
                return False

            wallet.state = "active"
            wallet.last_exhausted = None

            if self._active_wallet_id is None:
                self._active_wallet_id = wallet.id

            if self.auto_create:
                self._persist_to_disk(reason=f"wallet_{wallet_id}_reinstated")
            return True

    def set_active_wallet(self, wallet_id: str) -> bool:
        """Explicitly switch the active wallet pointer to the specified ID."""
        with self._lock:
            self._sync_from_disk_if_changed()
            if wallet_id not in self._wallets:
                return False

            self._active_wallet_id = wallet_id
            if self.auto_create:
                self._persist_to_disk(reason=f"set_active_wallet_{wallet_id}")
            return True

    def check_cooldowns(self, now: Optional[float] = None) -> int:
        """Check and recover any exhausted wallets that have completed their cooldown."""
        with self._lock:
            self._sync_from_disk_if_changed()
            recovered = self._apply_cooldowns_locked(now)
            if recovered > 0 and self.auto_create:
                self._persist_to_disk(reason="cooldown_check_recovery")
            return recovered

    def reload_from_disk(self, force: bool = True) -> bool:
        """Explicitly force a reload of the keystore from disk."""
        with self._lock:
            return self._sync_from_disk_if_changed(force=force)

    def save_to_disk(self) -> None:
        """Explicitly trigger an atomic save to disk."""
        with self._lock:
            self._persist_to_disk(reason="manual_save_to_disk")
