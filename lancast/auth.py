"""Pairing codes, remembered-device tokens, rate limiting and owner approval.

Threat model (see docs/SECURITY.md): anyone on the LAN can reach the server,
so every viewer must prove it saw the PC's screen (PIN or QR token) or hold a
device token issued after such a proof. Secrets are compared in constant time;
device tokens are stored only as SHA-256 digests.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("lancast.auth")


def new_id(prefix: str, nbytes: int = 6) -> str:
    return prefix + secrets.token_hex(nbytes)


def clean_text(value, limit: int) -> str:
    """Printable, single-line text: drops control/format characters (incl. bidi overrides)."""
    if not isinstance(value, str):
        return ""
    kept = "".join(ch for ch in value[: limit * 4] if unicodedata.category(ch)[0] != "C")
    return " ".join(kept.split())[:limit]


def clean_name(value, default: str = "未命名设备", limit: int = 40) -> str:
    return clean_text(value, limit) or default


def _ascii_eq(given, expected: str) -> bool:
    """Constant-time comparison that tolerates junk input types."""
    if not isinstance(given, str) or not given.isascii() or len(given) > 256:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


# --------------------------------------------------------------------------- pairing

class Pairing:
    """A single-use 6-digit PIN plus a 128-bit QR token that rotate together."""

    def __init__(
        self,
        ttl_s: float = 600.0,
        max_failures: int = 5,
        on_rotate: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s = float(ttl_s)
        self.max_failures = int(max_failures)
        self._on_rotate = on_rotate
        self._clock = clock
        self.pin = ""
        self.qr_token = ""
        self.expires_at = 0.0
        self.failures = 0
        self.rotate("start")

    def rotate(self, reason: str) -> None:
        self.pin = f"{secrets.randbelow(1_000_000):06d}"
        self.qr_token = secrets.token_urlsafe(16)
        self.expires_at = self._clock() + self.ttl_s
        self.failures = 0
        if self._on_rotate is not None:
            self._on_rotate(reason)

    def _refresh(self) -> None:
        if self._clock() >= self.expires_at:
            self.rotate("expired")

    def expires_in(self) -> int:
        self._refresh()
        return max(0, math.ceil(self.expires_at - self._clock()))

    def current(self) -> tuple[str, str]:
        self._refresh()
        return self.pin, self.qr_token

    def check_pin(self, pin) -> bool:
        self._refresh()
        ok = isinstance(pin, str) and len(pin) == 6 and pin.isascii() and pin.isdigit() and _ascii_eq(pin, self.pin)
        if ok:
            self.rotate("used")
            return True
        self.failures += 1
        if self.failures >= self.max_failures:
            self.rotate("too_many_failures")
        return False

    def check_qr(self, token) -> bool:
        self._refresh()
        if _ascii_eq(token, self.qr_token):
            self.rotate("used")
            return True
        return False


# --------------------------------------------------------------------------- rate limiting

class RateLimiter:
    """Blocks a key (source IP) for ``block_s`` after ``max_failures`` within ``window_s``."""

    def __init__(
        self,
        max_failures: int = 5,
        window_s: float = 300.0,
        block_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 4096,
    ) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self.block_s = block_s
        self._clock = clock
        self._max_entries = max_entries
        self._fails: dict[str, list[float]] = {}
        self._blocked: dict[str, float] = {}

    def retry_after(self, key: str) -> int:
        until = self._blocked.get(key)
        if until is None:
            return 0
        remaining = until - self._clock()
        if remaining <= 0:
            del self._blocked[key]
            return 0
        return max(1, math.ceil(remaining))

    def failure(self, key: str) -> None:
        now = self._clock()
        recent = [t for t in self._fails.get(key, ()) if now - t < self.window_s]
        recent.append(now)
        if len(recent) >= self.max_failures:
            self._blocked[key] = now + self.block_s
            self._fails.pop(key, None)
        else:
            self._fails[key] = recent
        self._prune(now)

    def success(self, key: str) -> None:
        self._fails.pop(key, None)

    def _prune(self, now: float) -> None:
        if len(self._fails) + len(self._blocked) <= self._max_entries:
            return
        self._blocked = {k: v for k, v in self._blocked.items() if v > now}
        self._fails = {k: v for k, v in self._fails.items() if v and now - v[-1] < self.window_s}
        while len(self._fails) + len(self._blocked) > self._max_entries and self._fails:
            self._fails.pop(next(iter(self._fails)))


# --------------------------------------------------------------------------- devices

@dataclass
class Device:
    device_id: str
    name: str
    token_sha256: str
    created_at: float
    last_seen_at: float
    last_ip: str
    ua: str = ""
    can_control: bool = False     # may this device send mouse/keyboard input


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class DeviceStore:
    """Remembered devices, persisted as JSON. Only token digests are stored."""

    MAX_DEVICES = 100

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._devices: dict[str, Device] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for item in raw.get("devices", []):
                dev = Device(**{k: item[k] for k in Device.__dataclass_fields__ if k in item})
                if not (dev.device_id.startswith("d_") and len(dev.token_sha256) == 64):
                    raise ValueError(f"bad device record {dev.device_id!r}")
                self._devices[dev.device_id] = dev
        except Exception as exc:  # noqa: BLE001 - never crash on a damaged file
            backup = self.path.with_name(f"{self.path.stem}.corrupt-{int(time.time())}.json")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            self._devices.clear()
            log.warning("device list was unreadable (%s); moved it to %s", exc, backup.name)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "devices": [asdict(d) for d in self._devices.values()]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def issue(self, name: str, ip: str, ua: str, can_control: bool = False) -> tuple[Device, str]:
        token = secrets.token_urlsafe(32)
        now = time.time()
        dev = Device(new_id("d_"), name, _digest(token), now, now, ip, ua, bool(can_control))
        self._devices[dev.device_id] = dev
        while len(self._devices) > self.MAX_DEVICES:
            oldest = min(self._devices.values(), key=lambda d: d.last_seen_at)
            del self._devices[oldest.device_id]
        self._save()
        return dev, token

    def verify(self, device_id, token) -> Device | None:
        if not isinstance(device_id, str) or not isinstance(token, str):
            return None
        if not token.isascii() or len(token) > 128 or len(device_id) > 64:
            return None
        dev = self._devices.get(device_id)
        # Same amount of work whether or not the id exists.
        ok = hmac.compare_digest(_digest(token), dev.token_sha256 if dev else "0" * 64)
        return dev if dev is not None and ok else None

    def touch(self, device_id: str, ip: str, ua: str = "") -> None:
        dev = self._devices.get(device_id)
        if dev is None:
            return
        dev.last_seen_at = time.time()
        dev.last_ip = ip
        if ua:
            dev.ua = ua
        self._save()

    def set_control(self, device_id: str, allowed: bool) -> bool:
        dev = self._devices.get(device_id)
        if dev is None:
            return False
        if dev.can_control != bool(allowed):
            dev.can_control = bool(allowed)
            self._save()
        return True

    def revoke(self, device_id: str) -> bool:
        if self._devices.pop(device_id, None) is None:
            return False
        self._save()
        return True

    def get(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    def list(self) -> list[Device]:
        return sorted(self._devices.values(), key=lambda d: d.last_seen_at, reverse=True)


# --------------------------------------------------------------------------- approvals

@dataclass
class ApprovalRequest:
    request_id: str
    name: str
    ip: str
    ua: str
    method: str
    created: float
    future: "asyncio.Future[str]"


class Approvals:
    """New devices wait here until the PC owner clicks Allow or Deny."""

    def __init__(
        self,
        timeout_s: float = 60.0,
        max_pending: int = 5,
        on_change: Callable[[ApprovalRequest, str], None] | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self.max_pending = max_pending
        self._on_change = on_change
        self._pending: dict[str, ApprovalRequest] = {}

    def busy_for(self, ip: str) -> bool:
        """True if another request is already queued for this IP, or the queue is full."""
        return len(self._pending) >= self.max_pending or any(r.ip == ip for r in self._pending.values())

    async def wait(self, name: str, ip: str, ua: str, method: str) -> str:
        """Returns "allow", "allow_control", "deny" or "timeout".

        Cancelling the caller withdraws the request."""
        loop = asyncio.get_running_loop()
        req = ApprovalRequest(new_id("r_", 4), name, ip, ua, method, time.time(), loop.create_future())
        self._pending[req.request_id] = req
        self._notify(req, "new")
        try:
            return await asyncio.wait_for(req.future, self.timeout_s)
        except asyncio.TimeoutError:
            return "timeout"
        finally:
            self._pending.pop(req.request_id, None)
            self._notify(req, "closed")

    def resolve(self, request_id: str, allow: bool, control: bool = False) -> bool:
        req = self._pending.get(request_id)
        if req is None or req.future.done():
            return False
        req.future.set_result(("allow_control" if control else "allow") if allow else "deny")
        return True

    def pending(self) -> list[ApprovalRequest]:
        return sorted(self._pending.values(), key=lambda r: r.created)

    def _notify(self, req: ApprovalRequest, what: str) -> None:
        if self._on_change is not None:
            try:
                self._on_change(req, what)
            except Exception:  # noqa: BLE001
                log.exception("approval callback failed")
